"""Recovery crash child (used by `tests/test_recovery.py`). Not a test module.

Usage: python tests/_recovery_child.py <data_dir> <writer_uuid_hex>

Runs one recovery with `BLACKBOX_DIE_AT` already set by the parent, so it dies at that step and leaves exactly the
state a `kill -9` would leave: markers for the steps it finished, nothing for the step it died in.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from blackbox.rec import recovery


def main(argv: list[str]) -> int:
    data_dir, uuid_hex = argv[1], argv[2]
    result = recovery.recover(data_dir, uuid_hex)
    print(result.capsule_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
