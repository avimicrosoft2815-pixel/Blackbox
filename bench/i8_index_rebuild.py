"""I8, measured: a 500-run corpus, an incremental index, a rebuild from nothing, and the two dumps compared.

    PYTHONPATH=src python bench/i8_index_rebuild.py [--runs 500] [--out bench/results/...json]

The acceptance line is *"rebuild and compare canonical dumps over a 500-run corpus"*. `tests/test_index_rebuild.py`
asserts it; this file records the numbers so a reader can see what it cost rather than take the word "fast".

What is measured, and what each number is:

* `corpus.runs` — real tapes, each sealed by `recovery.recover` in a directory of its own (one directory for all 500 makes
  the seal-time link resolver read every capsule and turns 25 s of work into 144 s);
* `incremental.*` — indexing the capsules in the order they are discovered, into a fresh file;
* `rebuild.*` — deleting the index and doing exactly that again;
* `dump.*` — the sha256 of the canonical dump after each, and whether they are equal. This is the property, not a proxy
  for it: the dump has no timings and no absolute paths, so equal hashes mean equal rows.

Nothing here writes into the repository except the JSON it is told to write.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.index import build as index_build
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter


def hardware() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "machine": os.uname().machine if hasattr(os, "uname") else "unknown",
        "sqlite": __import__("sqlite3").sqlite_version,
        "cpus": os.cpu_count(),
    }


def one_run(scratch: Path, run: int) -> str:
    """Write one tape and seal it; returns the capsule path."""
    writer = TapeWriter(str(scratch), segment_bytes=C.MIN_SEGMENT_BYTES)
    writer.batch_bytes = 0
    writer.batch_ms = 0.0
    assert writer.open() is writer
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": f"r_{run:016x}", "source": "i8"}), priority=0)
        writer.append(C.KIND_NODE_OPEN, cbor.encode({"name": "step", "kind": "step"}),
                      node_slot=1, priority=0, run_slot=1)
        for index in range(3):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"model": "m", "n": index}),
                          node_slot=1, priority=0, run_slot=1,
                          wall_ns=1_700_000_000_000_000_000 + index * 1_000_000)
        writer.append(C.KIND_NODE_CLOSE, cbor.encode({"status": "ok"}), node_slot=1, priority=0, run_slot=1)
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.publish()
    finally:
        writer.close()
    recovery.recover(str(scratch), writer.writer_uuid.hex())
    capsules = sorted(Path(scratch, "capsules").glob("*.bbx"))
    assert len(capsules) == 1, capsules
    return str(capsules[0])


def build_corpus(root: Path, runs: int) -> tuple[Path, float]:
    data_dir = root / "corpus"
    (data_dir / "capsules").mkdir(parents=True)
    started = time.perf_counter()
    for run in range(runs):
        scratch = root / f"run-{run:04d}"
        scratch.mkdir()
        capsule = Path(one_run(scratch, run))
        shutil.move(str(capsule), data_dir / "capsules" / capsule.name)
        shutil.rmtree(scratch, ignore_errors=True)
    return data_dir, (time.perf_counter() - started) * 1000.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="i8_index_rebuild")
    ap.add_argument("--runs", type=int, default=500)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-16-i8-index-rebuild.json"))
    ap.add_argument("--keep", help="build the corpus here instead of a temporary directory")
    args = ap.parse_args(argv)

    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="i8-"))
    if args.keep:
        root.mkdir(parents=True, exist_ok=True)
    try:
        data_dir, corpus_ms = build_corpus(root, args.runs)
        corpus_bytes = sum(p.stat().st_size for p in (data_dir / "capsules").glob("*.bbx"))

        started = time.perf_counter()
        incremental = index_build.build(data_dir)
        incremental_ms = (time.perf_counter() - started) * 1000.0
        incremental_hash = index_build.dump_hash(data_dir)
        incremental_rows = index_build.canonical_dump(data_dir).count(b"\n")

        started = time.perf_counter()
        rebuilt = index_build.rebuild(data_dir)
        rebuild_ms = (time.perf_counter() - started) * 1000.0
        rebuilt_hash = index_build.dump_hash(data_dir)
        rebuilt_rows = index_build.canonical_dump(data_dir).count(b"\n")
        index_bytes = Path(incremental["path"]).stat().st_size

        # Idempotence: a second incremental pass must add nothing to an index that already holds every capsule.
        again = index_build.build(data_dir)
        document = {
            "schema": "blackbox.bench/1",
            "bench": "i8-index-rebuild",
            "date": "2026-11-16",
            "hardware": hardware(),
            "corpus": {"runs": args.runs, "capsule_bytes": corpus_bytes, "build_ms": round(corpus_ms, 1),
                       "sealing": "recovery.recover, one tape per directory (see the module docstring)"},
            "incremental": {"capsules_seen": incremental["capsules_seen"], "rows_added": incremental["rows_added"],
                            "counts": incremental["counts"], "elapsed_ms": round(incremental_ms, 1)},
            "rebuild": {"capsules_seen": rebuilt["capsules_seen"], "rows_added": rebuilt["rows_added"],
                        "counts": rebuilt["counts"], "elapsed_ms": round(rebuild_ms, 1)},
            "reindex": {"rows_added": again["rows_added"]},
            "dump": {"incremental_sha256": incremental_hash, "rebuilt_sha256": rebuilt_hash,
                     "equal": incremental_hash == rebuilt_hash,
                     "rows": {"incremental": incremental_rows, "rebuilt": rebuilt_rows}},
            "index": {"path": "index/main.sqlite", "bytes": index_bytes,
                      "bytes_per_capsule": round(index_bytes / max(args.runs, 1), 1),
                      "unreadable": rebuilt["unreadable"], "disagreements": rebuilt["disagreements"]},
            "verdict": "i8 holds on this corpus" if incremental_hash == rebuilt_hash else "i8 VIOLATED",
        }
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: document[k] for k in ("corpus", "incremental", "rebuild", "reindex", "dump", "verdict")},
                     indent=2, sort_keys=True))
    if not document["dump"]["equal"]:
        print("blackbox: the rebuild did not reproduce the incremental dump", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
