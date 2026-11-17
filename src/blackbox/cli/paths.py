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
