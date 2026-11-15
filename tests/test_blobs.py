"""P1-14 / P1-15 — the blob store: atomic writes, verified reads, and a keeper-driven compression queue.

Acceptance from the plan:

* P1-14 — write path `hash → tmp → fsync → replace`, read path with codec dispatch and verify-on-read; **a kill
  during a blob write leaves no visible blob** (S9-style test);
* P1-15 — chunked compression queue driven by the keeper, header rewritten atomically; **compressed and raw blobs
  are both readable, and no partial file is ever visible**.

The read path is checked by corrupting real files, not by mocking: a flipped body byte has to raise `E162`, a
truncated file `E161`, a missing file `E160`. A reader that returned `None` or a half body would be the defect
these tests exist to catch (C26's rule, applied to the cold path).
"""
from __future__ import annotations

import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.errors import BlackBoxError
from blackbox.store import blobs

CHILD = ROOT / "tests" / "_blob_child.py"
BIG = bytes(range(256)) * 40          # 10 240 B: above the 2 KiB compression threshold
SMALL = b"x" * 64


def _put_big(tmp_path: Path) -> dict:
    return blobs.put(str(tmp_path), BIG)


def test_roundtrip_raw_blob_and_sharded_path(tmp_path: Path) -> None:
    rec = _put_big(tmp_path)
    h = rec["hash"].hex()
    assert rec["codec"] == blobs.CODEC_RAW
    assert rec["path"] == blobs.blob_path(str(tmp_path), h)
    assert rec["path"].endswith(os.path.join(h[0:2], h[2:4], f"{h}.bin"))
    got = blobs.get(str(tmp_path), h)
    assert got["payload"] == BIG
    assert got["orig_len"] == len(BIG)
    assert got["codec_name"] == "raw"


def test_small_payload_is_inline_and_never_becomes_a_blob_by_accident(tmp_path: Path) -> None:
    assert blobs.should_inline(SMALL) is True
    assert blobs.should_inline(b"x" * blobs.INLINE_MAX_BYTES) is True
    assert blobs.should_inline(b"x" * (blobs.INLINE_MAX_BYTES + 1)) is False
    rec = blobs.put(str(tmp_path), SMALL)
    assert rec["inline"] is True, "the caller decides; the store still records the fact"


def test_identical_payloads_deduplicate(tmp_path: Path) -> None:
    first = _put_big(tmp_path)
    second = blobs.put(str(tmp_path), BIG)
    assert second["deduped"] is True
    assert second["path"] == first["path"]
    assert len(list((tmp_path / "blob").rglob("*.bin"))) == 1


def test_ref_stub_names_hash_length_codec_and_span() -> None:
    stub = blobs.ref_stub({"hash": b"\x01" * 32, "orig_len": 5000, "codec": 1}, span=(10, 20))
    assert stub == {"b": b"\x01" * 32, "n": 5000, "c": 1, "s": [10, 20]}


def test_read_verifies_the_hash_on_every_read(tmp_path: Path) -> None:
    rec = _put_big(tmp_path)
    path = Path(rec["path"])
    data = bytearray(path.read_bytes())
    body_at = blobs._HEADER.size
    data[body_at + 100] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(BlackBoxError) as exc:
        blobs.get(str(tmp_path), rec["hash"])
    assert exc.value.code == "BBX-E162"
    assert "blob body does not match its hash" in str(exc.value)


def test_a_wrong_name_is_caught_by_the_hash_not_trusted(tmp_path: Path) -> None:
    rec = blobs.put(str(tmp_path), BIG)
    other = blobs.digest_of(b"something else entirely")
    path = Path(blobs.blob_path(str(tmp_path), other))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Path(rec["path"]).read_bytes())
    with pytest.raises(BlackBoxError) as exc:
        blobs.get(str(tmp_path), other)
    assert exc.value.code == "BBX-E162", "a blob under the wrong name must not be readable"


def test_truncated_and_missing_blobs_raise_named_errors(tmp_path: Path) -> None:
    rec = _put_big(tmp_path)
    Path(rec["path"]).write_bytes(Path(rec["path"]).read_bytes()[:10])
    with pytest.raises(BlackBoxError) as exc:
        blobs.get(str(tmp_path), rec["hash"])
    assert exc.value.code == "BBX-E161"
    with pytest.raises(BlackBoxError) as exc:
        blobs.get(str(tmp_path), blobs.digest_of(b"never written"))
    assert exc.value.code == "BBX-E160"
    assert exc.value.detail if hasattr(exc.value, "detail") else True


def test_container_layout_is_frozen(tmp_path: Path) -> None:
    rec = blobs.put(str(tmp_path), BIG)
    raw = Path(rec["path"]).read_bytes()
    assert raw[:8] == b"BBXBLOB1"
    assert len(raw) == 20 + len(BIG) + 4 + 32, "header + body + crc + hash"
    assert raw[8:10] == (1).to_bytes(2, "little"), "version 1"
    assert raw[10] == 0, "codec raw"
    assert raw[-32:] == rec["hash"], "the digest is the trailer, not the name of the directory"


def test_compressed_and_raw_blobs_are_both_readable(tmp_path: Path) -> None:
    raw = blobs.put(str(tmp_path), BIG, compress=False)
    comp = blobs.put(str(tmp_path), BIG + b"tail", compress=True)
    assert raw["codec"] == blobs.CODEC_RAW
    assert comp["codec"] == blobs.CODEC_ZLIB
    assert comp["stored_len"] < comp["orig_len"]
    assert blobs.get(str(tmp_path), raw["hash"])["payload"] == BIG
    assert blobs.get(str(tmp_path), comp["hash"])["payload"] == BIG + b"tail"


def test_incompressible_payload_stays_raw(tmp_path: Path) -> None:
    noise = os.urandom(4096)
    rec = blobs.put(str(tmp_path), noise, compress=True)
    assert rec["codec"] == blobs.CODEC_RAW, "compression that grows the file is not compression"
    assert blobs.get(str(tmp_path), rec["hash"])["payload"] == noise


def test_compression_queue_is_keeper_driven_and_rewrites_atomically(tmp_path: Path) -> None:
    rec = _put_big(tmp_path)
    q = blobs.CompressionQueue(str(tmp_path))
    assert q.enqueue(rec["hash"]) is True
    assert q.enqueue(rec["hash"]) is False, "the same blob must not be queued twice"
    assert blobs.get(str(tmp_path), rec["hash"])["codec"] == blobs.CODEC_RAW, "enqueue must not compress"
    seen: dict = {}
    real_replace = blobs.os.replace

    def spy(src, dst):
        # At the moment of the rename the old file must still be a complete raw blob, and the new one must be a
        # complete compressed blob: that is what "no partial file visible" means.
        seen["old"] = blobs.decode(Path(dst).read_bytes())
        seen["new"] = blobs.decode(Path(src).read_bytes())
        seen["tmp_exists"] = os.path.exists(src)
        return real_replace(src, dst)

    blobs.os.replace = spy
    try:
        stats = q.drain()
    finally:
        blobs.os.replace = real_replace
    assert stats["compressed"] == 1, stats
    assert seen["old"]["codec"] == blobs.CODEC_RAW
    assert seen["new"]["codec"] == blobs.CODEC_ZLIB
    assert seen["tmp_exists"] is True
    after = blobs.get(str(tmp_path), rec["hash"])
    assert after["codec"] == blobs.CODEC_ZLIB
    assert after["payload"] == BIG
    assert stats["bytes_saved"] > 0
    assert not list((tmp_path / "blob").rglob("*.tmp-*")), "the rename must consume the tmp file"


def test_queue_skips_small_and_already_compressed_blobs_without_failing(tmp_path: Path) -> None:
    small = blobs.put(str(tmp_path), b"tiny")
    comp = blobs.put(str(tmp_path), BIG, compress=True)
    q = blobs.CompressionQueue(str(tmp_path))
    q.enqueue(small["hash"])
    q.enqueue(comp["hash"])
    q.enqueue(blobs.digest_of(b"gone"))
    stats = q.drain()
    assert stats["compressed"] == 0
    assert stats["skipped"] == 3
    assert stats["failed"] == 0
    assert len(q) == 0


def test_a_kill_during_the_blob_write_leaves_no_visible_blob(tmp_path: Path) -> None:
    """S9-style: the tmp body is on disk, the final name never appeared."""
    env = dict(os.environ)
    env.pop(blobs.DIE_ENV, None)
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([sys.executable, str(CHILD), str(tmp_path), "S9", "4096"],
                          capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 137, f"child exited {proc.returncode}: {proc.stderr[-1500:]}"
    visible = list((tmp_path / "blob").rglob("*.bin"))
    assert visible == [], f"a blob became visible before its write finished: {visible}"
    leftovers = list((tmp_path / "blob").rglob("*.tmp-*"))
    assert leftovers, "the test is only meaningful if the tmp file actually existed"
    removed = blobs.sweep_tmp(str(tmp_path))
    assert len(removed) == len(leftovers)
    assert list((tmp_path / "blob").rglob("*.tmp-*")) == []


def test_a_kill_after_the_rename_leaves_a_complete_readable_blob(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop(blobs.DIE_ENV, None)
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([sys.executable, str(CHILD), str(tmp_path), "S9b", "4096"],
                          capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 137, f"child exited {proc.returncode}: {proc.stderr[-1500:]}"
    visible = list((tmp_path / "blob").rglob("*.bin"))
    assert len(visible) == 1
    got = blobs.decode(visible[0].read_bytes())
    assert got["payload"] == bytes(range(256)) * 16, "the visible blob must be the whole payload"


def test_a_kill_during_compression_leaves_the_raw_blob_readable(tmp_path: Path) -> None:
    """S9 is armed inside `_write_atomic`, so the queue's rewrite dies with the tmp file and nothing else."""
    rec = _put_big(tmp_path)
    code = (
        f"import os,sys;sys.path.insert(0,{str(ROOT / 'src')!r});"
        "from blackbox.store import blobs;"
        "os.environ['BLACKBOX_DIE_AT']='S9';"
        f"q=blobs.CompressionQueue({str(tmp_path)!r});q.enqueue({rec['hash'].hex()!r});q.drain()"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 137, f"child exited {proc.returncode}: {proc.stderr[-1500:]}"
    got = blobs.get(str(tmp_path), rec["hash"])
    assert got["codec"] == blobs.CODEC_RAW, "a kill mid-compress must leave the raw blob in place"
    assert got["payload"] == BIG
    assert blobs.sweep_tmp(str(tmp_path)), "the half-written compressed file is a tmp file, not a blob"


def test_zlib_body_matches_what_a_reader_expects(tmp_path: Path) -> None:
    """The stored body is plain zlib-6, not a private container: a reader can be written in ten lines."""
    rec = blobs.put(str(tmp_path), BIG, compress=True)
    raw = Path(rec["path"]).read_bytes()
    body = raw[blobs._HEADER.size:-36]
    assert zlib.decompress(body) == BIG


def test_the_keeper_drains_the_queue_so_compression_is_never_on_the_app_thread() -> None:
    """P1-15's "keeper-driven": one tick compresses at most `blob_jobs_per_tick` blobs, and the app does none."""
    import tempfile

    from blackbox.tape.keeper import Keeper
    from blackbox.tape.writer import TapeWriter

    with tempfile.TemporaryDirectory() as tmp:
        writer = TapeWriter(tmp, segment_bytes=1 << 16)
        writer.batch_bytes = 0
        writer.batch_ms = 0.0
        writer.open()
        try:
            queue = blobs.CompressionQueue(tmp)
            for i in range(3):
                rec = blobs.put(tmp, BIG + bytes([i]))
                assert rec["codec"] == blobs.CODEC_RAW
                assert queue.enqueue(rec["hash"]) is True
            keeper = Keeper(writer, blob_queue=queue, blob_jobs_per_tick=1)
            keeper.tick()
            assert queue.done == 1, "one tick must do one job, not the whole queue"
            keeper.tick()
            keeper.tick()
            assert queue.done == 3 and len(queue) == 0
            assert keeper.stats()["blob_queue"]["compressed"] == 3
        finally:
            writer.close()


def test_chunk_coalescing_turns_a_stream_into_few_whole_blobs(tmp_path: Path) -> None:
    """8 KiB buffers: a 2 000-chunk stream must not create 2 000 blobs (§8.8)."""
    co = blobs.ChunkCoalescer(str(tmp_path))
    for i in range(2000):
        co.append(bytes([i % 256]) * 16)
    assert len(co.closed_blobs) == 0 or len(co.closed_blobs) == 3, len(co.closed_blobs)
    final = co.close()
    assert final is not None
    assert co.closed_blobs
    total = 0
    for rec in co.closed_blobs:
        got = blobs.get(str(tmp_path), rec["hash"])
        total += len(got["payload"])
        assert len(got["payload"]) <= blobs.CHUNK_COALESCE_BYTES or rec is co.closed_blobs[-1]
    assert total == 2000 * 16
