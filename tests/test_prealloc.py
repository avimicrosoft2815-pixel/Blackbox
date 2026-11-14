"""P1-02/P1-20: segments are fully allocated and allocation failure degrades instead of raising SIGBUS."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox.errors import BlackBoxError
from blackbox.tape import prealloc, segments
from blackbox.tape.writer import TapeWriter


def test_preallocate_then_not_sparse(tmp_path):
    path = tmp_path / "x.bin"
    method = prealloc.preallocate(str(path), 4 << 20)
    assert path.stat().st_size == 4 << 20
    if hasattr(os, "posix_fallocate"):
        assert method in ("posix_fallocate", "write-zeros")
    assert prealloc.is_sparse(str(path), 4 << 20) is False
    prealloc.ensure_not_sparse(str(path), 4 << 20)


def test_sparse_detection(tmp_path):
    path = tmp_path / "sparse.bin"
    with open(path, "wb") as fh:
        fh.truncate(8 << 20)
    assert prealloc.is_sparse(str(path), 8 << 20) is True
    with pytest.raises(BlackBoxError):
        prealloc.ensure_not_sparse(str(path), 8 << 20)


def test_enospc_at_open_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv(prealloc.FAULT_ENV, "ENOSPC")
    with pytest.raises(BlackBoxError) as exc:
        TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    assert exc.value.code == "BBX-E132"    # preallocation failure, not the proxy allowlist code


def test_enospc_at_roll_records_degraded_flag(tmp_path, monkeypatch):
    w = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    w.append_and_publish(5, b"a" * 100)
    monkeypatch.setenv(prealloc.FAULT_ENV, "ENOSPC")
    with pytest.raises(BlackBoxError):
        w.roll()
    assert w.degraded_reason == "NO_SPACE"
    head = w.head
    assert head.flags & 1, "head must carry the DEGRADED_NO_SPACE flag"


def test_segment_header_roundtrip(tmp_path):
    path = segments.create(str(tmp_path), 1, writer_uuid=b"\x01" * 16, segment_bytes=64 * 1024,
                           prev_chain=b"\x00" * 32)
    with open(path, "rb") as fh:
        head = segments.decode_header(fh.read(4096))
    assert head["segment_no"] == 1
    assert head["segment_bytes"] == 64 * 1024
    assert os.path.getsize(path) == 64 * 1024
