"""`python -m blackbox` — the smallest honest CLI until P5 builds the real one.

`--version` prints the package version and the format version; nothing else is implemented yet, and saying so
is better than a stub that pretends. Exit code 2 for an unknown command matches argparse's convention.
"""
from __future__ import annotations

import argparse
import sys

from blackbox import __version__
from blackbox import constants as C


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blackbox", description="BlackBox recorder (pre-alpha: storage layer only)")
    ap.add_argument("--version", action="version",
                    version=f"blackbox {__version__} (format v{C.FORMAT_VERSION})")
    ap.add_argument("command", nargs="?", default="status",
                    help="`status` prints the build line; `run -- cmd` runs a command under the recorder")
    args, rest = ap.parse_known_args(argv)
    if args.command == "run":
        from blackbox.rec.launcher import main as run_main
        return run_main(rest)
    if args.command == "status":
        print(f"blackbox {__version__}: pre-alpha, storage and recovery layer only; "
              f"no SDK, proxy or viewer yet.  Tape format v{C.FORMAT_VERSION}.")
        print("See docs/STATUS.md for what is green, what is pending and what is not testable here.")
        return 0
    print(f"unknown command {args.command!r}; this build implements --version, status and run",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
