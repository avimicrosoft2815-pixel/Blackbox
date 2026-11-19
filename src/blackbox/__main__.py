"""`python -m blackbox` — the smallest honest CLI until P5 builds the real one.

`--version` prints the package version and the format version. Each command owns its own parser and its own `--help`, and
this front door passes the remaining arguments through untouched — which is why `blackbox repair verify --help` shows the
repair parser's help rather than this one's. Exit code 2 for an unknown command matches argparse's convention.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable

from blackbox import __version__
from blackbox import constants as C

HELP = ("`status` prints the build line; `run -- cmd` runs a command under the recorder; "
        "`desktop [path]` opens the viewer in a window of its own; "
        "`view <capsule>` writes one offline HTML page; `serve <capsule>` serves it on loopback once; "
        "`diagnose <capsule>` reports what a capsule's evidence supports; "
        "`repair preview|apply|verify` reviews and applies a proposal under an approval token; "
        "`ls` lists the capsules in a data directory; "
        "`link <a> <b>` declares that two tapes describe one run; `verify <c>|--run|--writer` checks a capsule's "
        "parts against its own frames; "
        "`ingest` is the command a coding agent's hook runs; `hook install|remove|status` manages the hook adapters")


# name -> (module, attribute). Nothing is imported until a command is actually chosen: the hook path (`ingest`) is on
# the agent's critical path and used to pay for ten modules it never runs — measured on this box, importing the whole
# table cost 72 ms of a 101 ms cold start, `blackbox.ui.server` alone dragging in `http.server`, `ssl`, `email`, `html`,
# `socketserver` and `mimetypes` (defect 64). The dispatch is a name, and a name costs nothing until it is imported.
COMMANDS: dict[str, tuple[str, str]] = {
    "run": ("blackbox.rec.launcher", "main"),
    "ingest": ("blackbox.cli.ingest", "main"),
    "hook": ("blackbox.cli.hook", "main"),
    "view": ("blackbox.ui.viewer", "main"),
    "serve": ("blackbox.ui.server", "main"),
    "diagnose": ("blackbox.diagnose.cli", "main"),
    "repair": ("blackbox.repair.cli", "main"),
    "link": ("blackbox.cli.link", "main"),
    "capsule": ("blackbox.cap.cli", "main"),
    "verify": ("blackbox.cli.cmd_verify", "main"),
    "ls": ("blackbox.cli.cmd_ls", "main"),
    "desktop": ("blackbox.ui.desktop", "main"),
}


def _load(name: str) -> Callable[[list[str]], int]:
    """Import one command's entry point. A command that cannot import itself must not stop another one."""
    module, attribute = COMMANDS[name]
    entry: Callable[[list[str]], int] = getattr(importlib.import_module(module), attribute)
    return entry


KNOWN = ("run", "ingest", "hook", "view", "serve", "diagnose", "repair", "link", "capsule", "verify", "ls",
         "desktop")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `blackbox repair verify --help` must show the repair parser's help, and `parse_known_args` would answer for this
    # parser first (it owns `-h`). So a help request after a known command is handed to that command before parsing.
    for index, token in enumerate(raw):
        if token in KNOWN:
            if any(flag in ("-h", "--help") for flag in raw[index + 1:]):
                return _load(token)(raw[index + 1:])
            break
    ap = argparse.ArgumentParser(prog="blackbox", description="BlackBox recorder (pre-alpha)")
    ap.add_argument("--version", action="version",
                    version=f"blackbox {__version__} (format v{C.FORMAT_VERSION})")
    ap.add_argument("command", nargs="?", default="status", help=HELP)
    args, rest = ap.parse_known_args(raw)
    if args.command in COMMANDS:
        return _load(args.command)(rest)
    if args.command == "status":
        print(f"blackbox {__version__}: pre-alpha. Storage, recovery, the capture seams, the loopback proxy, the "
              f"launcher's attach, the offline viewer, the read-only diagnosis, the interruption lab and the "
              f"approval-gated repair are built; nothing applies a fix without a token.  "
              f"Tape format v{C.FORMAT_VERSION}.")
        print("See docs/STATUS.md for what is green, what is pending and what is not testable here.")
        return 0
    print(f"unknown command {args.command!r}; this build implements --version, status, run, desktop, view, serve, "
          f"diagnose, repair, link, capsule, verify, ls, ingest and hook", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
