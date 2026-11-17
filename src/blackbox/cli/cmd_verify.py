"""`blackbox verify` — one command, three ways to name a target, five checks (P4-16).

    blackbox verify data/capsules/c_<id>.bbx
    blackbox verify --run r_0123456789abcdef --data DIR
    blackbox verify --writer <uuid> --data DIR

Exit codes follow the plan: **0** every check passed, **9** a check failed (`BBX-E412` for a derived part, the capsule's
own integrity root for a raw one), **2** the target cannot be read or does not exist. A check the capsule cannot answer —
`capture_vantage`, because a capsule carries no `meta` page — is printed as `declared` and is neither a pass nor a
failure; `--json` carries `ok: null, checkable: false` so a script cannot mistake it for one.

`--writer` is a *check*, not a lookup: the manifest has no writer field, so the command recomputes each capsule's id from
the writer it was asked about and that capsule's own run id (`cap.manifest.capsule_id_for`) and takes the ones that
match. A capsule whose declared id does not match its own derivation is reported rather than silently skipped — that
mismatch is itself a finding.

Every line this file prints is ASCII. A Windows console is cp1252, and a report that dies on a `!=` glyph (or a `…`) is a
verifier that cannot report a finding on the platform where someone needs it — the CI run that caught this is recorded in
`docs/STATE.json`'s `last_green_commit_note`.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from typing import Any

from blackbox.cap import format as cap
from blackbox.cap import manifest as manifest_mod
from blackbox.cli.paths import default_data_dir


def _capsules_in(data_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(data_dir, "capsules", "*.bbx")))


def _resolve(args: argparse.Namespace) -> tuple[list[str], str | None]:
    """The capsule paths to verify, or an error string. `--run`/`--writer` search `capsules/` and explain a miss."""
    if args.capsule:
        return [args.capsule], None
    data_dir = default_data_dir(args.data)
    paths = _capsules_in(data_dir)
    if not paths:
        return [], f"no capsules under {os.path.join(data_dir, 'capsules')}"
    wanted: list[str] = []
    mismatch: list[str] = []
    for path in paths:
        try:
            capsule = cap.read_capsule(path)
        except Exception:
            continue
        run_id = str((capsule.manifest.get("run") or {}).get("run_id") or "")
        declared_id = str(capsule.manifest.get("capsule_id") or "")
        if args.run:
            if run_id == args.run:
                wanted.append(path)
        elif args.writer:
            expected = manifest_mod.capsule_id_for(run_id, args.writer) if run_id else ""
            if expected and expected == declared_id:
                wanted.append(path)
            elif expected and declared_id and expected != declared_id and declared_id == os.path.basename(path)[:-4]:
                # The id in the file name matches the manifest, but not the derivation for this writer: someone is
                # claiming a tape it did not come from. Report it instead of quietly returning "no capsules".
                mismatch.append(f"{os.path.basename(path)}: declares {run_id} but its id is not {expected} for writer "
                                f"{args.writer}")
    if args.writer and not wanted:
        detail = ("; ".join(mismatch) if mismatch else
                  f"no capsule under {data_dir} derives from writer {args.writer}")
        return [], detail
    if not wanted:
        return [], f"no capsule under {data_dir} names run {args.run}"
    return wanted, None


def _print(path: str, report: dict[str, Any]) -> None:
    print(f"{path}")
    for check in report["checks"]:
        if check["ok"] is True:
            mark = "ok  "
        elif check["ok"] is False:
            mark = "FAIL"
        else:
            mark = "----"
        extra = ""
        if check["ok"] is False and not check.get("problems"):
            # With problems, those *are* the finding: they name the entry, the declared hash and the actual one. Printing
            # two truncated JSON blobs on top of them would bury the name in noise (and, for the integrity check, print
            # two identical roots, because an edited entry does not move the root over the index).
            extra = f"  declared {json.dumps(check['declared'], sort_keys=True)[:160]} " \
                    f"!= recomputed {json.dumps(check['recomputed'], sort_keys=True)[:160]}"
        elif check["ok"] is None and check.get("note"):
            extra = f"  {check['note']}"
        print(f"  {mark} {check['check']}{extra}")
        for problem in check.get("problems") or []:
            print(f"       {problem}")
    for drift in (report.get("drift") or []):
        print(f"       BBX-E412 {drift['part']}.{drift['field']}: "
              f"{json.dumps(drift.get('stored', drift.get('declared')), sort_keys=True)[:120]} != "
              f"{json.dumps(drift.get('recomputed', drift.get('actual')), sort_keys=True)[:120]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox verify",
        description="Verify a capsule against its own bytes: integrity root, chain root, derived parts, capture seam and "
                    "links summary. `capture_vantage` is printed as declared: a capsule carries no meta page, so it "
                    "cannot be recomputed and is not counted as a pass. Exit 0 verified, 9 a check failed, 2 unreadable.")
    ap.add_argument("capsule", nargs="?", help="path to a .bbx capsule")
    ap.add_argument("--run", help="verify every capsule of this run id under --data")
    ap.add_argument("--writer", help="verify the capsule the given writer uuid derives (checked, not looked up)")
    ap.add_argument("--data", help="data directory to search with --run/--writer (default $BLACKBOX_DATA_DIR, "
                                   "$BLACKBOX_HOME, or the current directory, which is where `blackbox run` writes)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if not args.capsule and not (args.run or args.writer):
        ap.error("name a capsule, or pass --run/--writer with --data")

    paths, error = _resolve(args)
    if error:
        print(f"blackbox verify: {error}", flush=True)
        return 2
    bodies: list[dict[str, Any]] = []
    unreadable = 0
    for path in paths:
        try:
            capsule = cap.read_capsule(path)
        except Exception as exc:
            print(f"blackbox verify: cannot read {path}: {type(exc).__name__}: {exc}")
            unreadable += 1
            continue
        report = manifest_mod.verify(capsule)
        parts = [check for check in report["checks"] if check["check"] == "derived_parts"]
        report["drift"] = (parts[0]["recomputed"].get("drift") if parts and isinstance(parts[0]["recomputed"], dict)
                           else [])
        bodies.append({"capsule": path, **report})
        if not args.json:
            _print(path, report)
    if unreadable and not bodies:
        return 2
    if args.json:
        print(json.dumps({"reports": bodies, "ok": all(body["ok"] for body in bodies) and not unreadable}, indent=2,
                         sort_keys=True))
    failed = [body["capsule"] for body in bodies if not body["ok"]]
    if failed or unreadable:
        if not args.json:
            for path in failed:
                print(f"blackbox verify: {path} failed a check")
            if unreadable:
                print(f"blackbox verify: {unreadable} capsule(s) could not be read")
        return manifest_mod.EXIT_DRIFT if failed else 2
    if not args.json:
        print(f"blackbox verify: {len(bodies)} capsule(s) verified; "
              f"{len(bodies[0]['uncheckable']) if bodies else 0} check(s) declared, not checkable from a capsule")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
