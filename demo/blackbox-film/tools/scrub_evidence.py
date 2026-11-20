#!/usr/bin/env python3
"""Turn a raw demo work directory into the evidence files committed with the film.

    python3 tools/scrub_evidence.py /tmp/bbdemo3 evidence/

The recording is real, which is exactly why it cannot be committed as-is: the run summary carries this machine's
absolute paths and the process id, and the brief quotes the command that was typed. This script replaces all of that
with stable placeholders, splits the run summary into a machine-readable `run.json` and the app's own `run.out`, and
then *verifies its own output* — if a home path or a bare pid survives anywhere, it exits non-zero rather than
committing a leak.

What is deliberately kept: run and capsule ids, sequence numbers, byte counts, timings, exit codes, the diagnosis and
the report text. Those are the evidence. Removing them would make the film's claims unverifiable, which is worse.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

TEXT_SUFFIXES = {".json", ".md", ".txt", ".log", ".err", ".out", ".srt"}
PLACEHOLDER_TMP = "<tmp>"
PLACEHOLDER_REPO = "<repo>"


def build_patterns(repo_root: Path | None) -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    if repo_root is not None:
        patterns.append((re.compile(re.escape(str(repo_root))), PLACEHOLDER_REPO))
    patterns.append((re.compile(r"/home/[A-Za-z0-9._-]+(?:/[^\s\"']*)?"), PLACEHOLDER_REPO))
    patterns.append((re.compile(r"/tmp/[A-Za-z0-9._-]+(?:/[^\s\"']*)?"), PLACEHOLDER_TMP))
    patterns.append((re.compile(r"blackbox-sdk-[0-9a-f]{6,}"), "<sdk-scratch>"))
    patterns.append((re.compile(r"blackbox-[0-9a-f]{6,}"), "<run-scratch>"))
    return patterns


def scrub_text(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def sanitise_run_summary(raw: str) -> tuple[dict, str]:
    """`run.json` is the app's stdout followed by its summary line; split them and drop host identity."""
    lines = [line for line in raw.splitlines() if line.strip()]
    summary = None
    for index in range(len(lines) - 1, -1, -1):
        candidate = lines[index].strip()
        if candidate.startswith("{"):
            try:
                summary = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            lines = lines[:index]
            break
    if summary is None:
        raise SystemExit("no run summary found in run.json")

    summary.pop("pid", None)
    argv = summary.get("argv")
    if isinstance(argv, list) and argv:
        summary["argv"] = ["<python>"] + [Path(str(part)).name for part in argv[1:]]

    for key in ("inbox", "capsule_path", "tape", "capsule_dir"):
        value = summary.get(key)
        if isinstance(value, str):
            summary[key] = re.sub(r"^.*/data/(.*)$", r"<work-dir>/data/\1", value)
    recovered = summary.get("recovered") or {}
    for key in ("capsule_path", "capsule_dir", "tape"):
        value = recovered.get(key)
        if isinstance(value, str):
            recovered[key] = re.sub(r"^.*/data/(.*)$", r"<work-dir>/data/\1", value)
    hooks = summary.get("hooks") or {}
    if isinstance(hooks.get("inbox"), str):
        hooks["inbox"] = re.sub(r"^.*/data/(.*)$", r"<work-dir>/data/\1", hooks["inbox"])

    return summary, "\n".join(lines) + ("\n" if lines else "")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    if not source.is_dir():
        raise SystemExit(f"no such work directory: {source}")
    target.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    patterns = build_patterns(repo_root)

    written: list[Path] = []
    for name in ("ls.txt", "verify.txt", "report.md", "brief.md", "diagnosis.json", "run.err", "provider.log", "diagnose.log"):
        origin = source / name
        if not origin.exists():
            continue
        text = scrub_text(origin.read_text(encoding="utf-8", errors="replace"), patterns)
        if name == "diagnosis.json":
            text = json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
        destination = target / name
        destination.write_text(text, encoding="utf-8")
        written.append(destination)

    run_raw = (source / "run.json").read_text(encoding="utf-8", errors="replace")
    summary, stdout = sanitise_run_summary(run_raw)
    summary_text = scrub_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", patterns)
    (target / "run.json").write_text(summary_text, encoding="utf-8")
    written.append(target / "run.json")
    if stdout.strip():
        (target / "run.out").write_text(scrub_text(stdout, patterns), encoding="utf-8")
        written.append(target / "run.out")

    # Copy nothing else: no capsules, no tape, no screenshots. The evidence directory is text a reviewer can read.
    leaks: list[str] = []
    for path in written:
        text = path.read_text(encoding="utf-8")
        for pattern, label in (
            (re.compile(r"/home/[A-Za-z0-9._-]+"), "absolute home path"),
            (re.compile(r"/tmp/[A-Za-z0-9._-]+"), "absolute temp path"),
            (re.compile(r'"pid"\s*:'), "process id"),
        ):
            if pattern.search(text):
                leaks.append(f"{path.name}: {label}")

    for path in written:
        print(f"wrote {path.relative_to(target.parent) if target.parent in path.parents else path}  ({path.stat().st_size} B)")
    if leaks:
        print("\nrefusing to finish — these files still leak:", file=sys.stderr)
        for leak in leaks:
            print(f"  {leak}", file=sys.stderr)
        return 1
    print("\nno home paths, temp paths or pids remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
