"""Run a command, echo its output, and put the tail of it in the GitHub job summary (C27).

Job *logs* on GitHub need a login; the run **Summary** page does not. So every CI step that can fail runs
through this wrapper: it prints the output as usual, appends a `<details>` block with the last 150 lines to
`$GITHUB_STEP_SUMMARY`, and exits with the command's own exit code. It is one file rather than a shell pipeline
because the Windows runner's default shell is PowerShell, where `2>&1 | tee` is not the POSIX thing.

Usage:  python tools/ci_summary.py --title "py3.12 / ubuntu · unit" -- pytest -q -rfE --tb=short
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TAIL = 150


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print(__doc__)
        return 2
    split = argv.index("--")
    head, cmd = argv[1:split], argv[split + 1:]
    title = "step"
    if "--title" in head:
        title = head[head.index("--title") + 1]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(out)
    sys.stdout.flush()

    lines = out.splitlines()
    tail = "\n".join(lines[-TAIL:])
    # C27 follow-up: the step summary is JS-rendered and the job log needs a login, but a `::error::` workflow
    # command becomes an ANNOTATION, which `gh run view` prints without any of that. One annotation per failed
    # test id, so a red cell names its tests from the CLI.
    for line in lines:
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            print(f"::error title={title}::{line.strip()[:200]}", flush=True)
    status = "passed" if proc.returncode == 0 else f"**failed** (exit {proc.returncode})"
    block = (f"\n### `{title}` — {status}\n\n"
             f"```\n{tail}\n```\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(block)
    else:                                   # local run: keep the same shape on stdout
        sys.stdout.write(block)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
