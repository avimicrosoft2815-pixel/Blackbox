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

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape.writer import TapeWriter

APPENDS = 100_000
WATCHED = ("write", "pwrite", "writev", "pwritev", "fsync", "fdatasync", "open", "openat",
           "posix_fallocate", "ftruncate", "lseek")


posix = pytest.mark.skipif(os.name != "posix", reason="counts POSIX syscall wrappers")
pytestmark = posix


class _SyncCounter:
    """Counts syncs **where they happen**, not where one OS happens to implement them (C36).

    Since C32/D-019 `platform.<shim>.fdatasync` and `.fsync_dir` are the only sync chokepoints (the AST guard in
    `tests/test_platform_dispatch.py` enforces it), so that is what a "syncs happen, and only at commit boundaries"
    claim has to count. Counting `os.fdatasync`/`os.fsync` was blind on macOS, where `fdatasync` issues
    `fcntl(F_FULLFSYNC)` and never touches either: the run said "0 syncs for 4000 events" and looked like a bug in
    the writer. On Darwin the `fcntl` path is counted as well, so a direct call that bypassed the shim is seen.
    """

    def __init__(self, monkeypatch) -> None:
        from blackbox import platform

        self.monkeypatch = monkeypatch
        self.plat = platform.current()
        self.counts = {"n": 0, "where": ""}
        self._unpatch: list[tuple[object, str, object]] = []

    def __enter__(self) -> dict:
        names: list[str] = []
        for name in ("fdatasync", "fsync_dir"):
            real = getattr(self.plat, name)
            self._patch(self.plat, name, real)
            names.append(name)
        self.counts["where"] = ", ".join(f"platform.{name}" for name in names)
        if sys.platform == "darwin":                      # pragma: no cover - exercised by the macOS cells
            import fcntl

            full = getattr(fcntl, "F_FULLFSYNC", None)
            if full is not None:
                real_fcntl = fcntl.fcntl

                def fcntl_counting(fd, cmd, *args, **kwargs):
                    if cmd == full:
                        self.counts["n"] += 1
                    return real_fcntl(fd, cmd, *args, **kwargs)

                self._unpatch.append((fcntl, "fcntl", real_fcntl))
                self.monkeypatch.setattr(fcntl, "fcntl", fcntl_counting)
                self.counts["where"] += ", fcntl(F_FULLFSYNC)"
        return self.counts

    def _patch(self, holder, name: str, real) -> None:
        def counting(*args, **kwargs):
            self.counts["n"] += 1
            return real(*args, **kwargs)

        self._unpatch.append((holder, name, real))
        self.monkeypatch.setattr(holder, name, counting)

    def __exit__(self, *exc) -> None:
        for holder, name, real in reversed(self._unpatch):
            self.monkeypatch.setattr(holder, name, real)


def count_syncs(monkeypatch) -> _SyncCounter:
    return _SyncCounter(monkeypatch)


def test_process_mode_append_path_makes_no_syscall(tmp_path, monkeypatch):
    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="process",
                   chain_enabled=True).open()
    w.batch_bytes = C.DEFAULT_BATCH_BYTES          # group commit: publish every 16 KiB, no fsync in process mode
    payload = b"n" * 64
    for _ in range(1_000):                         # warm up, and let any lazy import finish
        w.emit(C.KIND_MODEL_REQUEST, payload)
    w.flush()

    counts = dict.fromkeys(WATCHED, 0)
    publishes_before = w.publishes

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
    publishes = w.publishes - publishes_before
    # C21: exactly TWO pwrites per batch inside the measured window — the segment data and the 64 B head slot —
    # and nothing else. The CHAIN frame rides in the data buffer; `flush` at the end publishes once more.
    assert made.get("pwrite", 0) == 2 * publishes, (
        f"{made.get('pwrite', 0)} pwrites for {publishes} publishes is not two per batch")
    w.close()


def test_kernel_mode_syncs_only_on_the_commit_boundary(tmp_path, monkeypatch):
    """The counterpart: kernel mode may sync, but only on the group-commit boundary, never per event."""
    # the sync bound is set small on purpose: the 1 MiB / 250 ms default would not fire inside a fast
    # burst, and "no sync happened" is not the same claim as "syncs are not per event"
    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="kernel",
                   sync_bytes=64 << 10).open()
    w.batch_bytes = C.DEFAULT_BATCH_BYTES
    w.batch_ms = 60_000.0                          # only the byte bound can publish
    with count_syncs(monkeypatch) as synced:
        payload = b"k" * 64
        events = 4_000
        for _ in range(events):
            w.emit(C.KIND_MODEL_REQUEST, payload)
        w.flush()
    assert 0 < synced["n"] < events // 10, (
        f"{synced['n']} syncs for {events} events (counted at the platform chokepoint: {synced['where']})")
    w.close()


def test_a_64kib_full_event_costs_a_bounded_named_set_of_calls(tmp_path, monkeypatch):
    """C32/§15.1: one `content=full` 64 KiB event in `process` mode, counted call by call.

    The row is a hard fail above 10 ms, so the point is what the *shape* of the cost is: four calls per new blob
    (open, write, close, replace — all into the page cache), two `pwrite`s per publish for the tape, and
    **no sync of any kind**. Before D-019 this cost two fsyncs of the calling thread per event.
    """
    from blackbox.store import blobs

    w = TapeWriter(str(tmp_path), segment_bytes=C.DEFAULT_SEGMENT_BYTES, mode="process",
                   chain_enabled=True).open()
    bw = blobs.BlobWriter(str(tmp_path), mode="process")
    payload = bytes(range(256)) * 256               # 64 KiB, incompressible-ish and not repeated
    blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, payload)   # warm up (meta/head, first blob)
    w.flush()

    watched = ("write", "pwrite", "writev", "pwritev", "fsync", "fdatasync", "open", "openat", "close",
               "replace", "posix_fallocate", "ftruncate", "lseek")
    counts = dict.fromkeys(watched, 0)

    def counting(name, real):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)
        return wrapper

    patched = {}
    for name in watched:
        real = getattr(os, name, None)
        if real is None:
            continue
        patched[name] = real
        monkeypatch.setattr(os, name, counting(name, real), raising=False)
    publishes_before = w.publishes
    try:
        with count_syncs(monkeypatch) as chokepoint:
            for i in range(20):
                blobs.append_payload(w, bw, C.KIND_MODEL_RESPONSE, payload + bytes([i]))
            w.flush()
    finally:
        for name, real in patched.items():
            monkeypatch.setattr(os, name, real, raising=False)

    made = {name: n for name, n in counts.items() if n}
    publishes = w.publishes - publishes_before
    assert counts["fsync"] == 0 and counts["fdatasync"] == 0, f"the calling thread synced: {made}"
    # The OS-level count is necessary but not sufficient: on macOS `fdatasync` is `fcntl(F_FULLFSYNC)`, which touches
    # neither `os.fsync` nor `os.fdatasync`. Counting the platform chokepoint closes that hole (C36).
    assert chokepoint["n"] == 0, f"a sync went through {chokepoint['where']} on the 64 KiB process-mode path"
    assert set(made) <= {"open", "pwrite", "close", "replace", "lseek", "ftruncate"}, (
        f"unexpected syscalls on the 64 KiB path: {made}")
    # 20 blobs, one file each: open + pwrite + close + replace. The tape adds 2 pwrites per publish.
    assert made.get("open", 0) == 20, made
    assert made.get("replace", 0) >= 20, made
    assert made.get("pwrite", 0) == 20 + 2 * publishes, made
    w.close()
