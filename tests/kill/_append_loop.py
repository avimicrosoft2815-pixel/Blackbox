"""Child for the randomized kill matrix (P2-09): appends and publishes in a tight loop until killed.

Usage: python tests/kill/_append_loop.py <data_dir> <mode> <segment_bytes> <event_bytes> <batch_bytes>
       [rate_per_s]

Prints `READY` on stdout once `meta`, `head` and the first segment exist, then appends until the parent kills
it with SIGKILL. C19: the parent starts its kill timer only after reading that line, so a trial that never got
started is a broken parent, not a `not_started` data point. `rate_per_s > 0` paces the loop, so the kill lands
inside the group-commit window at a low event rate instead of always mid-burst.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from blackbox import constants as C
from blackbox.tape.writer import TapeWriter


def main(argv: list[str]) -> int:
    data_dir, mode = argv[1], argv[2]
    segment_bytes, event_bytes, batch_bytes = int(argv[3]), int(argv[4]), int(argv[5])
    rate = float(argv[6]) if len(argv) > 6 else 0.0
    writer = TapeWriter(data_dir, segment_bytes=segment_bytes, mode=mode).open()
    writer.batch_bytes = batch_bytes
    payload = b"k" * event_bytes
    sys.stdout.write("READY\n")
    sys.stdout.flush()                            # meta + head + segment 1 exist before this line
    i = 0
    while True:                                   # the parent decides when this stops
        writer.emit(C.KIND_MODEL_REQUEST, payload)
        if batch_bytes == 0:
            writer.flush()
        i += 1
        if rate:                                  # low-rate cell: most of each interval is spent inside the window
            time.sleep(1.0 / rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
