"""P1-02/P1-20: segments are fully allocated and allocation failure degrades instead of raising SIGBUS."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox.errors import BlackBoxError
from blackbox.tape import prealloc, segments
from blackbox.tape.writer import TapeError, TapeWriter


def test_preallocate_then_not_sparse(tmp_path):
    path = tmp_path / "x.bin"
    method = prealloc.preallocate(str(path), 4 << 20)
    assert path.stat().st_size == 4 << 20
    if hasattr(os, "posix_fallocate"):
        assert method in ("posix_fallocate", "write-zeros")
    assert prealloc.is_sparse(str(path), 4 << 20) is False
    prealloc.ensure_not_sparse(str(path), 4 << 20)


@pytest.mark.skipif(
    os.name == "nt",
    reason="on NTFS, extending a file with SetEndOfFile (os.ftruncate) ALLOCATES the clusters; a file is "
           "sparse only after FSCTL_SET_SPARSE, so 'truncate makes it sparse' is false there, not unmeasurable")
def test_sparse_detection(tmp_path):
    path = tmp_path / "sparse.bin"
    with open(path, "wb") as fh:
        fh.truncate(8 << 20)
    assert prealloc.is_sparse(str(path), 8 << 20) is True
    with pytest.raises(BlackBoxError):
        prealloc.ensure_not_sparse(str(path), 8 << 20)


def test_enospc_at_open_degrades_to_a_sink(tmp_path, monkeypatch):
    """S17 covers the first segment: no segment means a sink, not an exception into the app."""
    monkeypatch.setenv(prealloc.FAULT_ENV, "ENOSPC")
    w = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()          # must not raise
    assert w.recording is False
    assert w.degraded_reason == "NO_SPACE"
    assert w.emit(5, b"payload") == 0                                       # dropped, counted
    assert w.dropped_events == 1
    assert w.flush() is None
    assert w.roll() is False
    with pytest.raises(TapeError):
        _ = w.head                     # no head exists, and the error says why
    w.close()
    monkeypatch.delenv(prealloc.FAULT_ENV)
    # nothing was published, so no reader can see a head naming a segment that does not exist
    assert not (Path(tmp_path) / "tape" / w.writer_uuid.hex() / "head").exists() or \
        __import__("blackbox.tape.head", fromlist=["x"]).read_best(
            str(Path(tmp_path) / "tape" / w.writer_uuid.hex() / "head")) is None


def test_enospc_at_roll_degrades_without_raising(tmp_path, monkeypatch):
    """S17 (C5): a preallocation failure at roll degrades the tape and never reaches the app."""
    from blackbox import constants as C

    w = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    w.append_and_publish(C.KIND_MODEL_REQUEST, b"a" * 100)
    segment_before = w.head.active_segment
    fd_before = w._seg_fd
    monkeypatch.setenv(prealloc.FAULT_ENV, "ENOSPC")
    assert w.roll() is False, "roll must report that it did not roll"
    monkeypatch.delenv(prealloc.FAULT_ENV)

    assert w.degraded_reason == "NO_SPACE"
    assert w.head.flags & C.HEAD_FLAG_DEGRADED_NO_SPACE
    assert w.head.active_segment == segment_before, "no roll happened"
    assert w._seg_fd == fd_before, "the writer stayed on the same segment"
    w.close()


def test_segment_header_roundtrip(tmp_path):
    path = segments.create(str(tmp_path), 1, writer_uuid=b"\x01" * 16, segment_bytes=64 * 1024,
                           prev_chain=b"\x00" * 32)
    with open(path, "rb") as fh:
        head = segments.decode_header(fh.read(4096))
    assert head["segment_no"] == 1
    assert head["segment_bytes"] == 64 * 1024
    assert os.path.getsize(path) == 64 * 1024
