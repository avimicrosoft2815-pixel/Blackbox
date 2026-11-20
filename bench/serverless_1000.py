"""P8-11, measured: 1 000 simulated invocations with random exceptions and timeouts leave no open run.

    PYTHONPATH=src python bench/serverless_1000.py [--invocations 1000] [--out bench/results/...json]

"Simulated" means the failures are injected where a platform injects them: an exception inside the handler (the ordinary
one), a `TimeoutError` raised from inside (what a Lambda timeout looks like to the code that is still running), and a
**killed** invocation — a tape and a marker with no seal, which is exactly what a process killed at the platform's limit
leaves behind, and which the next cold start has to recover.

The assertion is the acceptance line itself: after every invocation there is no open run — no tape that nothing sealed —
and every capsule the corpus holds verifies against its own root.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import serverless
from blackbox.cap import format as cap
from blackbox.tape.writer import TapeWriter


def _kill(data_dir: str, run: int) -> None:
    """Leave behind exactly what a process killed at the platform's limit leaves: a tape, no capsule, a marker."""
    writer = TapeWriter(data_dir, mode="serverless").open()
    writer.emit(0x50, b"\x00")
    writer.flush()
    writer.close()
    Path(data_dir, serverless.MARKER).write_text(json.dumps({"schema": "blackbox.serverless/1", "pid": 999_999}) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="serverless_1000")
    ap.add_argument("--invocations", type=int, default=1000)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-16-serverless-1000.json"))
    args = ap.parse_args(argv)

    rng = random.Random(20261116)
    root = Path(tempfile.mkdtemp(prefix="sls-"))
    data = str(root / "data")
    documents: list[dict] = []
    outcomes = {"ok": 0, "timeout": 0, "error": 0, "killed": 0}
    verified = 0
    failures: list[str] = []
    started = time.perf_counter()

    @serverless.wrap(on_capsule=documents.append, data_dir=data)
    def handler(event, context):
        roll = rng.random()
        if roll < 0.10:
            raise TimeoutError(f"invocation {event} exceeded its limit")
        if roll < 0.20:
            raise ValueError(f"invocation {event} got a bad payload")
        return {"echo": event}

    try:
        for invocation in range(args.invocations):
            if rng.random() < 0.05:                 # 5% are killed outright: this process never reaches its seal
                _kill(data, invocation)
                outcomes["killed"] += 1
                continue
            try:
                handler(invocation, None)
                outcomes["ok"] += 1
            except TimeoutError:
                outcomes["timeout"] += 1
            except ValueError:
                outcomes["error"] += 1
        elapsed_s = time.perf_counter() - started
        # The invariant, checked from the filesystem rather than from the documents the wrapper returned.
        open_runs = sorted(Path(data, "tape").glob("*"))
        capsules = sorted(Path(data, "capsules").glob("*.bbx"))
        for path in capsules:
            if not cap.read_capsule(str(path)).root_ok:
                failures.append(f"capsule does not verify: {path.name}")
            verified += 1
        unrecorded = [doc for doc in documents if doc["completeness"] != "sealed"]
        document = {
            "schema": "blackbox.bench/1",
            "bench": "serverless-1000",
            "date": "2026-11-16",
            "invocations": args.invocations,
            "outcomes": outcomes,
            "documents": len(documents),
            "recovered_invocations": sum(1 for doc in documents if doc.get("recovered")),
            "completeness": {kind: sum(1 for doc in documents if doc["completeness"] == kind)
                             for kind in sorted({doc["completeness"] for doc in documents})},
            "capsules": len(capsules),
            "capsules_verified": verified,
            "open_runs_after_the_run": len(open_runs),
            "marker_left_behind": os.path.exists(os.path.join(data, serverless.MARKER)),
            "unrecorded_invocations": len(unrecorded),
            "elapsed_s": round(elapsed_s, 1),
            "ms_per_invocation": round(elapsed_s * 1000.0 / args.invocations, 2),
            "failures": failures,
            "verdict": "no open run" if not open_runs and not failures else "OPEN RUN OR BROKEN CAPSULE",
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: document[key] for key in ("outcomes", "documents", "capsules", "capsules_verified",
                                                     "open_runs_after_the_run", "marker_left_behind", "elapsed_s",
                                                     "verdict")}, indent=2, sort_keys=True))
    return 0 if document["verdict"] == "no open run" else 1


if __name__ == "__main__":
    raise SystemExit(main())
