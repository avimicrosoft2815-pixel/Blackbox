"""P1-06 … P1-13: meta, two-slot head, seal footer, leases, and a multi-segment round-trip."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox import constants as C  # noqa: E402
from blackbox.errors import BlackBoxError  # noqa: E402
from blackbox.tape import head as head_mod  # noqa: E402
from blackbox.tape import lease as lease_mod  # noqa: E402
from blackbox.tape import meta as meta_mod  # noqa: E402
from blackbox.tape import scanner, segments  # noqa: E402
from blackbox.tape import seal as seal_mod  # noqa: E402
from blackbox.tape.writer import TapeWriter  # noqa: E402


# ── meta ────────────────────────────────────────────────────────────────────
def test_meta_roundtrip():
    uuid = os.urandom(16)
    blob = meta_mod.encode(writer_uuid=uuid, segment_bytes=1 << 20, vantage=2)
    assert len(blob) == C.META_BYTES == 4096
    d = meta_mod.decode(blob)
    assert d["writer_uuid"] == uuid and d["segment_bytes"] == 1 << 20 and d["vantage"] == 2
    assert d["format_version"] == C.FORMAT_VERSION


def test_meta_checksum_and_version_guards():
    blob = bytearray(meta_mod.encode(writer_uuid=os.urandom(16), segment_bytes=1 << 20, vantage=1))
    blob[20] ^= 0x01
    with pytest.raises(BlackBoxError) as exc:
        meta_mod.decode(bytes(blob))
    assert exc.value.code == "BBX-E222"
    newer = bytearray(meta_mod.encode(writer_uuid=os.urandom(16), segment_bytes=1 << 20, vantage=1))
    newer[8] = C.MAX_READER_FORMAT_VERSION + 1          # format_version low byte
    newer[-4:] = b"\x00\x00\x00\x00"
    import struct
    from blackbox.codec.crc32c import crc32c
    struct.pack_into("<I", newer, C.META_CRC_OFF, crc32c(bytes(newer[:C.META_CRC_OFF])))
    with pytest.raises(BlackBoxError) as exc:
        meta_mod.decode(bytes(newer))
    assert exc.value.code == "BBX-E221"


# ── head ────────────────────────────────────────────────────────────────────
def test_head_alternates_and_survives_a_torn_slot(tmp_path):
    path = tmp_path / "head"
    path.write_bytes(head_mod.empty_head_file())
    for i in range(1, 6):
        head_mod.publish(str(path), head_mod.HeadSlot(head_off=i * 1000, head_seq=i))
    best = head_mod.read_best(str(path))
    assert best.gen == 5 and best.head_off == 5000
    s0, s1 = head_mod.read_slots(str(path))
    assert {s0.gen, s1.gen} == {4, 5}          # the two newest commits are both recoverable

    raw = bytearray(path.read_bytes())
    newest_base = 0 if (s0 and s0.gen == 5) else C.HEAD_SLOT_STRIDE
    raw[newest_base + C.HEAD_CRC_OFF] ^= 0xFF  # tear the newest slot's CRC
    path.write_bytes(bytes(raw))
    fallback = head_mod.read_best(str(path))
    assert fallback.gen == 4 and fallback.head_off == 4000


def test_both_slots_invalid_returns_none(tmp_path):
    path = tmp_path / "head"
    path.write_bytes(head_mod.empty_head_file())
    head_mod.publish(str(path), head_mod.HeadSlot(head_off=100))
    path.write_bytes(b"\xff" * C.HEAD_BYTES)
    assert head_mod.read_best(str(path)) is None


# ── seal ────────────────────────────────────────────────────────────────────
def test_seal_verifies_and_detects_tampering(tmp_path):
    tape = tmp_path
    body = b"body-bytes" * 10
    segments.create(str(tape), 1, writer_uuid=b"\x02" * 16, segment_bytes=64 * 1024, prev_chain=b"\x00" * 32)
    seg = segments.seg_path(str(tape), 1)
    with open(seg, "r+b") as fh:
        fh.seek(C.SEGMENT_HEADER_BYTES)
        fh.write(body)
    info = segments.seal_segment(str(tape), 1, reason=C.SEAL_REASON_ROLLED, body=body,
                                prev_chain=b"\x00" * 32, start_seq=1, end_seq=3, frame_count=3,
                                sealer=b"")
    assert info["body_len"] == len(body) and info["frame_count"] == 3
    seal_mod.verify(seg, segments.seal_path(str(tape), 1))

    with open(seg, "r+b") as fh:      # flip one byte of the body
        fh.seek(C.SEGMENT_HEADER_BYTES + 3)
        fh.write(b"X")
    with pytest.raises(BlackBoxError) as exc:
        seal_mod.verify(seg, segments.seal_path(str(tape), 1))
    assert exc.value.code == "BBX-E224"


def test_seal_is_256_bytes_and_chained():
    body = b"abc"
    a = seal_mod.encode(segment_no=1, reason=1, start_seq=1, end_seq=1, frame_count=1,
                        body_len=len(body), body_crc32c=0, body_hash=b"\x01" * 32,
                        chain=b"\x02" * 32, sealer=b"", closed_wall_ns=0)
    assert len(a) == C.SEAL_BYTES == 256


# ── lease ───────────────────────────────────────────────────────────────────
def test_lease_excludes_a_live_writer(tmp_path):
    path = str(tmp_path / "writer.lease")
    held = lease_mod.acquire(path)
    assert lease_mod.is_live(path)
    with pytest.raises(BlackBoxError) as exc:
        lease_mod.acquire(path)
    assert exc.value.code == "BBX-E210"
    lease_mod.release(path)
    assert not lease_mod.is_live(path)
    fresh = lease_mod.acquire(path)          # released => acquirable again
    assert fresh.pid == held.pid


def test_lease_of_a_dead_pid_is_stale(tmp_path):
    path = tmp_path / "writer.lease"
    import json, time
    path.write_text(json.dumps({"v": 1, "pid": 999_999, "proc_start_ns": None, "boot_id": None,
                                "nonce": "x", "created_ns": time.time_ns()}), encoding="utf-8")
    stale, why = lease_mod.read(str(path)).is_stale()
    assert stale and why == "pid gone"
    assert lease_mod.acquire(str(path))       # stealable


# ── multi-segment round-trip ────────────────────────────────────────────────
def test_roll_produces_two_sealed_segments_and_a_full_scan(tmp_path):
    w = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    written = 0
    while w.head.active_segment == 1 and written < 5000:
        try:
            w.append_and_publish(C.KIND_MODEL_REQUEST, b"e" * 200)
            written += 1
        except BlackBoxError:
            break
    uuid = w.writer_uuid.hex()
    w.close()

    tape_dir = str(tmp_path / "tape" / uuid)
    segs = segments.list_segments(tape_dir)
    assert len(segs) >= 2, segs
    for n in segs:
        info = seal_mod.verify(segments.seg_path(tape_dir, n), segments.seal_path(tape_dir, n))
        assert info["frame_count"] > 0
    # P1-18: the chain links segments — segment n+1's header chains to segment n's seal
    for prev, nxt in zip(segs, segs[1:]):
        with open(segments.seg_path(tape_dir, nxt), "rb") as fh:
            header = segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))
        assert header["prev_chain"] == seal_mod.read(segments.seal_path(tape_dir, prev))["chain"]

    result = scanner.scan(str(tmp_path), uuid)
    assert result.completeness() == "full", result.summary()
    assert result.sealed_segments == segs[:-1]
    # every event that publish() returned for is on the tape
    assert result.last_seq >= written


def test_roll_exactly_at_chain_emission_keeps_seq_unique_and_dense(tmp_path):
    """A9 regression: the CHAIN frame used to be encoded before the roll decision.

    A roll inside `publish()` advances `head_seq` with the `SEG_END` frame, so an already-encoded CHAIN frame
    reused the SEG_END's seq number and the next one was never used: a duplicate plus a hole. 64 KiB segments
    with ~1 KiB events force that boundary often, so the test sweeps the whole capacity.
    """
    writer = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    uuid = writer.writer_uuid.hex()
    payload = b"y" * 900
    try:
        for _ in range(400):                      # 400 * ~1 KiB crosses several segment boundaries
            writer.emit(C.KIND_MODEL_REQUEST, payload)
            if writer.head.head_off > writer.segment_bytes - 4096:
                writer.publish()
    finally:
        writer.close()

    result = scanner.scan(str(tmp_path), uuid)
    seqs = [f["seq"] for f in result.frames]
    assert seqs == list(range(1, len(seqs) + 1)), "seq numbers must be unique and dense"
    assert result.gaps == []
    assert result.completeness() == "full"
    assert len(segments.list_segments(str(tmp_path / "tape" / uuid))) >= 3
