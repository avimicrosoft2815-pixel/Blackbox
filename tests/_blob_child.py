"""Blob-write crash child (used by `tests/test_blobs.py`). Not a test module.

Usage: python tests/_blob_child.py <data_dir> <stage> <payload_bytes>

Writes one blob with `BLACKBOX_DIE_AT=<stage>` armed, then prints the blob's hash. Dying at `S9` leaves the
temporary file on disk and **nothing visible** under the final name; dying at `S9b` leaves a visible blob whose
directory entry has not been fsynced. Both are states a `kill -9` can leave, and both are read back by the test.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from blackbox.store import blobs


def main(argv: list[str]) -> int:
    data_dir, stage, size = argv[1], argv[2], int(argv[3])
    os.environ[blobs.DIE_ENV] = stage
    payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
    record = blobs.put(data_dir, payload)
    print(record["hash"].hex())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
