"""C32 / D-019 — blob durability belongs to the keeper, and the order of the two syncs is the invariant.

`format.md` §7.1 used to have the calling thread fsync a blob and its directory, which made a 64 KiB event cost two
syncs of the user's thread. That is the wrong owner: a `process`-mode tape promises nothing against a *machine*
crash, so syncing a blob there buys nothing the tape is not already buying, and in `kernel`/`paranoid` the sync has
to be ordered against the tape's own sync anyway — the only thread that can order them is the one that publishes.

These tests hold the three modes to that, with a shim that records the calls in order (the `tests/test_no_syscall.py`
counting-shim form, but ordered rather than counted):

* `process`  — the calling thread syncs nothing at all, blob included;
* `kernel`   — every blob referenced by a durable frame was synced, and the blob sync came **before** the tape sync
  in the same tick;
* `paranoid` — the blob is durable before the referencing frame is appended, hence before it can be published.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.store import blobs
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter

posix = pytest.mark.skipif(os.name != "posix", reason="records the POSIX sync calls")
pytestmark = posix

BIG = 64 << 10


class Recorder:
    """A platform shim that forwards to the real one and remembers what was synced, in order, with the path."""

    def __init__(self, shim) -> None:
        # The real functions are captured here, before the fixture replaces the shim's attributes: reading them off
        # `shim` later would resolve to this recorder and recurse (the C36 lesson — count at the chokepoint, but
        # hold the real callable from before the swap).
        self.real = shim
        self._real_fdatasync = shim.fdatasync
        self._real_fsync_dir = shim.fsync_dir
        self._real_open_fd = shim.open_fd
        self.calls: list[tuple[str, str]] = []
        self._paths: dict[int, str] = {}

    def __getattr__(self, name):
        return getattr(self.real, name)

    def open_fd(self, path, flags, mode=0o600):
        fd = self._real_open_fd(path, flags, mode)
        self._paths[fd] = str(path)
        return fd

    def fdatasync(self, fd: int) -> None:
        self.calls.append(("fdatasync", self._paths.get(fd, f"fd:{fd}")))
        self._real_fdatasync(fd)

    def fsync_dir(self, path: str) -> None:
        self.calls.append(("fsync_dir", str(path)))
        self._real_fsync_dir(path)

    # ── queries ────────────────────────────────────────────────────────────
    def syncs(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("fdatasync", "fsync_dir")]

    def blob_syncs(self) -> list[tuple[str, str]]:
        return [c for c in self.syncs() if os.sep + "blob" + os.sep in c[1]]

    def tape_syncs(self) -> list[tuple[str, str]]:
        return [c for c in self.syncs() if c not in self.blob_syncs()]


@pytest.fixture()
def recorder(monkeypatch):
    """Count at the **platform chokepoint**, in place (C36).

    Replacing two modules' `_plat` references only caught the two modules that were named; `HeadFile` captures its
    shim in `__init__` and every other module holds the same module object. Patching the shim's own functions means
    no sync can happen anywhere without being recorded, and it is OS-independent: on Darwin the shim's `fdatasync`
    issues `fcntl(F_FULLFSYNC)` internally, which is exactly why counting `os.fdatsync` saw zero.
    """
    from blackbox import platform

    shim = platform.current()
    rec = Recorder(shim)
    monkeypatch.setattr(shim, "fdatasync", rec.fdatasync)
    monkeypatch.setattr(shim, "fsync_dir", rec.fsync_dir)
    monkeypatch.setattr(shim, "open_fd", rec.open_fd)
    return rec


def _writer(tmp_path, mode="process", **kw):
    return TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode=mode,
                      chain_enabled=True, **kw).open()


def test_process_mode_syncs_nothing_on_the_calling_thread(tmp_path, recorder):
    w = _writer(tmp_path, "process")
    bw = blobs.BlobWriter(str(tmp_path), mode="process")
    recorder.calls.clear()                     # open() creates meta/head; the claim is about the event path
    for i in range(5):
        blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, bytes([i]) * BIG)
    w.flush()
    assert recorder.syncs() == [], f"process mode synced on the event path: {recorder.syncs()}"
    w.close()


def test_kernel_mode_syncs_the_blob_before_the_tape(tmp_path, recorder):
    w = _writer(tmp_path, "kernel")
    w.batch_bytes = 512                      # the 8 stub frames (≈ 100 B each) make the tick's publish due
    w.sync_ms = 0                            # and the tape's own sync due, so the ordering is observable
    bw = blobs.BlobWriter(str(tmp_path), mode="kernel")
    keeper = Keeper(w, tick_s=3600.0, blob_writer=bw)          # ticks driven by hand, no thread
    w.attach_blobs(bw)
    for i in range(8):
        blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, bytes([i]) * BIG)
    assert bw.pending() == 8, "kernel mode must queue the blob for the keeper, not sync it inline"
    assert recorder.blob_syncs() == [], "the calling thread synced a blob in kernel mode"
    assert w.publishes == 0, "the batch must still be unpublished when the keeper ticks"
    recorder.calls.clear()                     # from here on: one tick, and the order inside it is the claim
    keeper.tick()
    assert bw.pending() == 0
    assert recorder.blob_syncs(), "the keeper published without syncing the blobs it made durable"
    blob_first = recorder.calls.index(recorder.blob_syncs()[0])
    tape = recorder.tape_syncs()
    assert tape, "no tape sync recorded: the ordering claim would be vacuous"
    tape_first = recorder.calls.index(tape[0])
    assert blob_first < tape_first, (
        f"the tape synced before the blob: {recorder.calls[:6]}")
    w.close()


def test_paranoid_mode_syncs_before_the_frame_is_appended(tmp_path, recorder, monkeypatch):
    w = _writer(tmp_path, "paranoid")
    bw = blobs.BlobWriter(str(tmp_path), mode="paranoid")
    appends: list[int] = []
    real_append = w.append

    def spy(kind, payload=b"", **kw):
        appends.append(len(recorder.blob_syncs()))
        return real_append(kind, payload, **kw)

    monkeypatch.setattr(w, "append", spy)
    blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, b"z" * BIG)
    assert len(appends) == 1
    # two syncs per new blob: the file, then its directory entry
    assert appends[0] == 2, f"the frame was appended after {appends[0]} blob syncs, expected 2"
    w.close()


def test_a_durable_frame_never_references_a_non_durable_blob(tmp_path, recorder):
    """The invariant, checked against the tape rather than against the shim: read the published frames back."""
    from blackbox.tape import reader

    w = _writer(tmp_path, "kernel")
    w.batch_bytes = 512
    w.sync_ms = 0
    bw = blobs.BlobWriter(str(tmp_path), mode="kernel")
    keeper = Keeper(w, tick_s=3600.0, blob_writer=bw)
    w.attach_blobs(bw)
    payloads = [bytes([i]) * BIG for i in range(6)]
    for p in payloads:
        blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, p)
    keeper.tick()
    synced = {path for _op, path in recorder.blob_syncs()}
    tape_dir = w.tape_dir
    w.close()

    frames, blobbed = list(reader.iter_tape(tape_dir)), 0
    for frame in frames:
        if not frame.get("flags", 0) & C.FRAME_FLAG_HAS_BLOB:
            continue
        blobbed += 1
        from blackbox.codec import cbor
        stub = cbor.decode_exact(frame["payload"])
        digest = stub["b"]
        path = blobs.blob_path(str(tmp_path), digest.hex())
        assert path in synced, f"frame seq {frame['seq']} is durable and its blob {path} was never synced"
        assert blobs.get(str(tmp_path), digest)["payload"] in payloads
    assert blobbed == len(payloads), f"{blobbed} frames carried a blob stub, expected {len(payloads)}"
