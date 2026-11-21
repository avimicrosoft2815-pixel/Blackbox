#!/usr/bin/env python3
"""Where the time in a 500-capsule fixture actually goes — evidence for defect 114, not a benchmark of the product.

Defect 114: three Windows CI cells were killed by the suite's 120-second per-test watchdog inside
`tests/test_index_rebuild.py`, on commits that touch no index code, while the same files passed on the other eight cells
and on the identical Windows job in a second run. The question that decides whether that is *our* problem or the
*runner's* is: what in building this corpus scales differently on NTFS?

This tool answers it from the phases rather than from an opinion. It builds the same corpus the fixture builds — real
tapes, sealed by the real recovery path, each in a scratch directory of its own (the fixture's note explains why) — and
times:

    tape      TapeWriter.open/append/publish/close          (the recording path)
    seal      recovery.recover(tape)                        (the work the fixture does 500 times)
    move      the capsule rename into the corpus

then repeats the whole build with the platform's sync calls replaced by no-ops and reports the difference. That
difference is the part of the fixture's cost that a Windows runner pays several times over: `FlushFileBuffers` plus a
Defender scan on every close, where this machine pays an ext4 `fdatasync` and nothing else.

It reports the number of sync calls per capsule too, because "syncs are expensive" is only interesting next to "how many
of them".

Run it where the artefact is wanted:

    PYTHONPATH=src python tools/capsule_write_cost.py --runs 50 --out bench/results/<date>-capsule-write-cost.json

Nothing here is a product measurement. It measures a *test's* fixture cost, on this host, and says so in the output.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C                                   # noqa: E402
from blackbox.codec import cbor                                       # noqa: E402
from blackbox.platform import current as current_platform             # noqa: E402
from blackbox.rec import recovery                                     # noqa: E402
from blackbox.tape.writer import TapeWriter                           # noqa: E402


def _tape(data_dir: Path, *, run_id: str, frames: int = 3) -> str:
    """One small tape, the same frame shape the index fixture writes: fixed wall clock, one node, a few requests."""
    writer = TapeWriter(str(data_dir), segment_bytes=C.MIN_SEGMENT_BYTES)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    assert writer.open() is writer
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "source": "cost-probe"}), priority=0)
        writer.append(C.KIND_NODE_OPEN, cbor.encode({"name": "step", "kind": "step"}),
                      node_slot=1, priority=0, run_slot=1)
        for index in range(frames):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"model": "m", "n": index}),
                          node_slot=1, priority=0, run_slot=1,
                          wall_ns=1_700_000_000_000_000_000 + index * 1_000_000)
        writer.append(C.KIND_NODE_CLOSE, cbor.encode({"status": "ok"}), node_slot=1, priority=0, run_slot=1)
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.publish()
    finally:
        writer.close()
    return writer.writer_uuid.hex()


@contextmanager
def _syncs_counted() -> Iterator[list[int]]:
    """Count the platform's sync calls without changing them: the observer must not be the thing measured."""
    module = current_platform()
    counter: list[int] = [0]
    originals = {name: getattr(module, name) for name in ("fdatasync", "fsync_dir") if hasattr(module, name)}
    for name, original in originals.items():
        def wrapper(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            counter[0] += 1
            return _original(*args, **kwargs)
        setattr(module, name, wrapper)
    try:
        yield counter
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


@contextmanager
def _syncs_disabled() -> Iterator[None]:
    """Replace the sync calls with no-ops. `fdatasync` still advances the *file* the way the caller expects to read it
    back, because the bytes are already in the page cache; only the durability step is skipped, which is the cost being
    isolated."""
    module = current_platform()
    originals = {name: getattr(module, name) for name in ("fdatasync", "fsync_dir") if hasattr(module, name)}
    for name in originals:
        setattr(module, name, lambda *args, **kwargs: None)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


def _build(root: Path, runs: int) -> dict[str, float]:
    """Build `runs` tapes and seal each in its own scratch directory; return the phase times in milliseconds."""
    data_dir = root / "corpus"
    (data_dir / "capsules").mkdir(parents=True, exist_ok=True)
    tape_ms: list[float] = []
    seal_ms: list[float] = []
    move_ms: list[float] = []
    for run in range(runs):
        scratch = root / f"run-{run:03d}"
        scratch.mkdir()
        started = time.perf_counter()
        _tape(scratch, run_id=f"r_{run:016x}")
        tape_ms.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        recovery.recover(str(scratch), None)
        seal_ms.append((time.perf_counter() - started) * 1000.0)
        capsule = next(Path(scratch, "capsules").glob("*.bbx"))
        started = time.perf_counter()
        capsule.rename(data_dir / "capsules" / capsule.name)
        move_ms.append((time.perf_counter() - started) * 1000.0)
    return {"tape_ms": tape_ms, "seal_ms": seal_ms, "move_ms": move_ms}


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "total_ms": round(sum(ordered), 3),
        "per_item_p50_ms": round(statistics.median(ordered), 3),
        "per_item_p95_ms": round(ordered[max(0, int(round(0.95 * (len(ordered) - 1))))], 3),
        "per_item_max_ms": round(ordered[-1], 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=50, help="tapes to build (the suite's fixture uses 500)")
    parser.add_argument("--out", help="write the JSON here")
    args = parser.parse_args(argv)

    machine = {
        "system": os.uname().sysname if hasattr(os, "uname") else sys.platform,
        "release": os.uname().release if hasattr(os, "uname") else "",
        "python": sys.version.split()[0],
        "cpus": os.cpu_count(),
    }

    with tempfile.TemporaryDirectory(prefix="bbx-cost-") as scratch:
        root = Path(scratch)
        started = time.perf_counter()
        with _syncs_counted() as counter:
            measured = _build(root, args.runs)
        wall_ms = (time.perf_counter() - started) * 1000.0
        sync_calls = counter[0]

    with tempfile.TemporaryDirectory(prefix="bbx-cost-nosync-") as scratch:
        root = Path(scratch)
        started = time.perf_counter()
        with _syncs_disabled():
            unsynced = _build(root, args.runs)
        unsynced_wall_ms = (time.perf_counter() - started) * 1000.0

    record = {
        "schema": "blackbox.capsule-write-cost/1",
        "what_this_measures": (
            "the cost of building the index fixture's corpus on this host, split by phase, and again with the platform's "
            "sync calls disabled. It is a test-fixture measurement for defect 114, not a product benchmark."),
        "machine": machine,
        "runs": args.runs,
        "with_syncs": {
            "wall_ms": round(wall_ms, 3),
            "sync_calls_per_capsule": round(sync_calls / max(1, args.runs), 2),
            **{phase: _summary(values) for phase, values in measured.items()},
        },
        "with_syncs_disabled": {
            "wall_ms": round(unsynced_wall_ms, 3),
            **{phase: _summary(values) for phase, values in unsynced.items()},
        },
        "sync_share_of_wall": round(1.0 - (unsynced_wall_ms / wall_ms), 4) if wall_ms else None,
        "extrapolated_to_the_fixture": {
            "500_capsules_ms_here": round(wall_ms / max(1, args.runs) * 500, 1),
            "note": ("linear extrapolation of this host's per-capsule cost; the fixture also moves each capsule into one "
                     "directory, which is not linear on every filesystem, so this is an order-of-magnitude figure"),
        },
        "what_a_windows_runner_would_have_to_pay": (
            "the same phases, with `FlushFileBuffers` per sync and a Defender scan per close. The sync share above is the "
            "part of this fixture whose cost differs most between ext4 and NTFS; the tape and seal phases are the same "
            "work everywhere."),
    }
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
