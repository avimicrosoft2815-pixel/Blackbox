"""`blackbox capsule verify <capsule>` — check the derived parts against the raw frames (D-053).

Read-only, like the diagnosis workflow: it opens a capsule, recomputes what can be recomputed, and prints what it found.
Exit 0 when nothing drifted, 1 when something did, 2 when the capsule cannot be opened at all — the same split the other
commands use, so a script can tell "bad input" from "a real finding".
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from blackbox.cap import derived
from blackbox.cap import format as cap


def _print_report(path: str, report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"capsule": path, **report}, indent=2, sort_keys=True))
        return
    for part in report["parts"]:
        declared = "declared" if part["declared"] else "NOT declared in manifest.derived_parts"
        print(f"  {part['name']}: sha256 {part['sha256'][:16]}… ({declared}, generator {part['generator'] or 'unknown'})")
    for name in report["absent"]:
        print(f"  {name}: absent — this capsule carries no derived graph, so there is nothing to check")
    for drift in report["drift"]:
        stored = drift.get("stored", drift.get("declared"))
        fresh = drift.get("recomputed", drift.get("actual"))
        print(f"  DRIFT {drift['part']}.{drift['field']}: found {json.dumps(stored, sort_keys=True)[:200]} "
              f"≠ recomputed {json.dumps(fresh, sort_keys=True)[:200]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox capsule",
        description="Verify a capsule's derived parts against the raw frames it carries. `verify` recomputes "
                    "`graph.json` from the segment bytes and `links.json`, and reports drift as BBX-E412. It never "
                    "rewrites the capsule: a verifier that repaired the artefact would erase the evidence it found.")
    ap.add_argument("action", choices=("verify",), help="what to do with the capsule")
    ap.add_argument("capsule", help="path to a .bbx capsule")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    try:
        capsule = cap.read_capsule(args.capsule)
    except Exception as exc:
        print(f"blackbox capsule: cannot read {args.capsule}: {type(exc).__name__}: {exc}")
        return 2
    if not capsule.root_ok:
        print("blackbox capsule: the capsule's own integrity root does not verify:")
        for problem in capsule.problems:
            print(f"  {problem}")
        return 2
    try:
        report = derived.verify(capsule)
    except Exception as exc:                              # BBX-E412 for a graph that is not even JSON, and any surprise
        print(f"blackbox capsule: {type(exc).__name__}: {exc}")
        return 1
    _print_report(args.capsule, report, as_json=args.json)
    if report["drift"]:
        if not args.json:
            print("blackbox capsule: derived part drifted from the raw frames (BBX-E412)")
        return 1
    if not args.json:
        print("blackbox capsule: derived parts verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
