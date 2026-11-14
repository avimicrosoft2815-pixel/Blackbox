"""Child for the randomized kill matrix (P2-09): appends and publishes in a tight loop until killed.

Usage: python tests/kill/_append_loop.py <data_dir> <mode> <segment_bytes> <event_bytes> <batch_bytes>
Prints nothing; the parent kills it with SIGKILL at a random delay.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from blackbox import constants as C
from blackbox.tape.writer import TapeWriter


def main(argv: list[str]) -> int:
    data_dir, mode = argv[1], argv[2]
    segment_bytes, event_bytes, batch_bytes = int(argv[3]), int(argv[4]), int(argv[5])
    writer = TapeWriter(data_dir, segment_bytes=segment_bytes, mode=mode).open()
    writer.batch_bytes = batch_bytes
    payload = b"k" * event_bytes
    i = 0
    while True:                                   # the parent decides when this stops
        writer.emit(C.KIND_MODEL_REQUEST, payload)
        if batch_bytes == 0:
            writer.flush()
        i += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
