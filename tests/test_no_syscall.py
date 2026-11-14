"""C11 / P1-08: "one syscall per batch" is a claim, so here is the evidence.

strace is not installed in this environment, so this is the counting-shim form the brief allows: the syscalls
a write path could use are wrapped and counted across 100 000 appends in `process` mode, after `open()` has
done its work. Since ADR-005 the store is `pwrite`-append, so the claim is **not** "no syscall": it is exactly
one `pwrite` per group-commit batch and nothing else — no per-event `write`, no `fsync`, no `lseek`, no `open`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape.writer import TapeWriter

APPENDS = 100_000
WATCHED = ("write", "pwrite", "writev", "pwritev", "fsync", "fdatasync", "open", "openat",
           "posix_fallocate", "ftruncate", "lseek")


def test_process_mode_append_path_makes_no_syscall(tmp_path, monkeypatch):
    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="process",
                   chain_enabled=True).open()
    w.batch_bytes = C.DEFAULT_BATCH_BYTES          # group commit: publish every 16 KiB, no fsync in process mode
    payload = b"n" * 64
    for _ in range(1_000):                         # warm up, and let any lazy import finish
        w.emit(C.KIND_MODEL_REQUEST, payload)
    w.flush()

    counts = dict.fromkeys(WATCHED, 0)

    def counting(name, real):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)
        return wrapper

    patched = {}
    for name in WATCHED:
        real = getattr(os, name, None)
        if real is None:
            continue
        patched[name] = real
        monkeypatch.setattr(os, name, counting(name, real), raising=False)
    try:
        for _ in range(APPENDS):
            w.emit(C.KIND_MODEL_REQUEST, payload)
        w.flush()
    finally:
        for name, real in patched.items():
            monkeypatch.setattr(os, name, real, raising=False)

    made = {name: n for name, n in counts.items() if n}
    assert set(made) <= {"pwrite"}, f"syscalls other than pwrite on the append path: {made}"
    batches = made.get("pwrite", 0)
    # 100 000 appends of 64 B payloads are ~13.7 MB, i.e. ~840 batches of 16 KiB: one pwrite each, plus the
    # CHAIN frame and SEG_END written through the same buffer. Anything near one call per event fails here.
    assert 1 <= batches <= APPENDS // 50, f"{batches} pwrites for {APPENDS} appends is not one per batch"
    w.close()


def test_kernel_mode_syncs_only_on_the_commit_boundary(tmp_path, monkeypatch):
    """The counterpart: kernel mode may sync, but only on the group-commit boundary, never per event."""
    # the sync bound is set small on purpose: the 1 MiB / 250 ms default would not fire inside a fast
    # burst, and "no sync happened" is not the same claim as "syncs are not per event"
    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="kernel",
                   sync_bytes=64 << 10).open()
    w.batch_bytes = C.DEFAULT_BATCH_BYTES
    w.batch_ms = 60_000.0                          # only the byte bound can publish
    synced = {"n": 0}
    real = os.fdatasync if hasattr(os, "fdatasync") else os.fsync

    def counting(fd):
        synced["n"] += 1
        return real(fd)

    monkeypatch.setattr(os, "fdatasync", counting, raising=False)
    monkeypatch.setattr(os, "fsync", counting, raising=False)
    payload = b"k" * 64
    events = 4_000
    for _ in range(events):
        w.emit(C.KIND_MODEL_REQUEST, payload)
    w.flush()
    monkeypatch.setattr(os, "fdatasync", real, raising=False)
    assert 0 < synced["n"] < events // 10, f"{synced['n']} syncs for {events} events"
    w.close()
