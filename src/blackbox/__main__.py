"""`python -m blackbox` — the smallest honest CLI until P5 builds the real one.

`--version` prints the package version and the format version. Each command owns its own parser and its own `--help`, and
this front door passes the remaining arguments through untouched — which is why `blackbox repair verify --help` shows the
repair parser's help rather than this one's. Exit code 2 for an unknown command matches argparse's convention.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from blackbox import __version__
from blackbox import constants as C

HELP = ("`status` prints the build line; `run -- cmd` runs a command under the recorder; "
        "`view <capsule>` writes one offline HTML page; `serve <capsule>` serves it on loopback once; "
        "`diagnose <capsule>` reports what a capsule's evidence supports; "
        "`repair preview|apply|verify` reviews and applies a proposal under an approval token; "
        "`ls` lists the capsules in a data directory; "
        "`link <a> <b>` declares that two tapes describe one run; `verify <c>|--run|--writer` checks a capsule's "
        "parts against its own frames; "
        "`ingest` is the command a coding agent's hook runs; `hook install|remove|status` manages the hook adapters")


def _dispatch_table() -> dict[str, Callable[[list[str]], int]]:
    """Imports are inside the functions: `--version` and `status` must work without loading the proxy, the SDK or numpy-
    free as they are, a command that cannot import itself should not stop `blackbox --version`."""
    from blackbox.cap.cli import main as capsule_main
    from blackbox.cli.cmd_ls import main as ls_main
    from blackbox.cli.cmd_verify import main as verify_main
    from blackbox.cli.hook import main as hook_main
    from blackbox.cli.ingest import main as ingest_main
    from blackbox.cli.link import main as link_main
    from blackbox.diagnose.cli import main as diagnose_main
    from blackbox.rec.launcher import main as run_main
    from blackbox.repair.cli import main as repair_main
    from blackbox.ui.server import main as serve_main
    from blackbox.ui.viewer import main as view_main

    return {"run": run_main, "ingest": ingest_main, "hook": hook_main, "view": view_main, "serve": serve_main,
            "diagnose": diagnose_main, "repair": repair_main, "link": link_main, "capsule": capsule_main,
            "verify": verify_main, "ls": ls_main}


KNOWN = ("run", "ingest", "hook", "view", "serve", "diagnose", "repair", "link", "capsule", "verify", "ls")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `blackbox repair verify --help` must show the repair parser's help, and `parse_known_args` would answer for this
    # parser first (it owns `-h`). So a help request after a known command is handed to that command before parsing.
    for index, token in enumerate(raw):
        if token in KNOWN:
            if any(flag in ("-h", "--help") for flag in raw[index + 1:]):
                return _dispatch_table()[token](raw[index + 1:])
            break
    ap = argparse.ArgumentParser(prog="blackbox", description="BlackBox recorder (pre-alpha)")
    ap.add_argument("--version", action="version",
                    version=f"blackbox {__version__} (format v{C.FORMAT_VERSION})")
    ap.add_argument("command", nargs="?", default="status", help=HELP)
    args, rest = ap.parse_known_args(raw)
    if args.command in _dispatch_table():
        return _dispatch_table()[args.command](rest)
    if args.command == "status":
        print(f"blackbox {__version__}: pre-alpha. Storage, recovery, the capture seams, the loopback proxy, the "
              f"launcher's attach, the offline viewer, the read-only diagnosis, the interruption lab and the "
              f"approval-gated repair are built; nothing applies a fix without a token.  "
              f"Tape format v{C.FORMAT_VERSION}.")
        print("See docs/STATUS.md for what is green, what is pending and what is not testable here.")
        return 0
    print(f"unknown command {args.command!r}; this build implements --version, status, run, view, serve, diagnose, "
          f"repair, link, capsule, verify, ls, ingest and hook", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
