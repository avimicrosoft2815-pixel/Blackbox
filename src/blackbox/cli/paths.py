"""Where the commands look for a data directory when nobody passed `--data`.

The front door had three answers to that question. `blackbox run` writes into the **current directory**
(`data_dir = os.path.abspath(BLACKBOX_HOME or ".")`), so a run leaves `./tape/` and `./capsules/` beside the program.
`blackbox ls` and `blackbox verify --data` looked in `./.blackbox` instead, so the next command a new user types after
`blackbox run` printed "no capsules" over a directory that had two in it — and `docs/onboarding.md`, which says
`ls capsules/`, only worked because it skipped the tool's own default.

One resolver, then: an explicit `--data` wins, then `$BLACKBOX_DATA_DIR`, then `$BLACKBOX_HOME` (the variable the
launcher exports to children), then the current directory — the place `run` put them.

It is not a search. A command that quietly walks up the tree to find a data directory reports on a directory the user did
not name, which is worse than saying there is nothing here.
"""
from __future__ import annotations

import os


def default_data_dir(explicit: str | None = None) -> str:
    """The data directory for a read-only command: `--data`, then `$BLACKBOX_DATA_DIR`, `$BLACKBOX_HOME`, then `.`."""
    return os.path.expanduser(explicit or os.environ.get("BLACKBOX_DATA_DIR") or os.environ.get("BLACKBOX_HOME") or ".")


# ── a path that arrived cut in half ─────────────────────────────────────────────────────────────────────────────────
# Defect 116. On a Windows runner `blackbox verify` was handed the string `D` where the calling script passed
# `D:\a\…\x.bbx`: the CLI reported `cannot read D` and the reader had no way to know that the rest of the path had never
# arrived. Nothing here can reconstruct a path that was cut before `argv`, and guessing would be worse than refusing — but
# the one shape that is unmistakable (a single letter on its own) can be named, with the two things that avoid it.
_DRIVE_ONLY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def looks_like_a_truncated_windows_path(path: str) -> bool:
    """`True` for a bare drive letter — `D`, `d` — and for nothing else.

    A one-character path that is a letter is not a capsule, a data directory or a file anybody meant to name. It is the
    left half of `D:\\…` after something cut at the colon, which is what defect 116 recorded.
    """
    return len(path) == 1 and path in _DRIVE_ONLY


def path_refusal_advice(path: str) -> str:
    """A sentence to append to a "cannot read this path" line, or the empty string when there is nothing to add."""
    if looks_like_a_truncated_windows_path(path):
        return (f" — `{path}` is a single drive letter, so the rest of the path never reached this command. On Windows a "
                f"path built and passed by a script can be cut at the colon (defect 116): quote it, or pass it from the "
                f"shell, or let Python find the file itself.")
    return ""
