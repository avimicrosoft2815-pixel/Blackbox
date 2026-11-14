"""Crash-injection child process (used by tests/test_crash_points.py). Not a test module.

Usage: python tests/_child.py <data_dir> <stage> <events> <die_on> <append|roll>

`BLACKBOX_DIE_AT` is only armed for the chosen event, so the tape already holds earlier commits when the
process dies — which is the situation the commit protocol has to survive. The child dies via `os._exit`,
exactly like `kill -9`: no atexit, no flush, no close.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from blackbox import constants as C
from blackbox.tape.writer import TapeWriter


def main(argv: list[str]) -> int:
    data_dir, stage, events, die_on = argv[1], argv[2], int(argv[3]), int(argv[4])
    mode = argv[5] if len(argv) > 5 else "append"
    writer = TapeWriter(data_dir, segment_bytes=64 * 1024, mode="process").open()
    for i in range(events):
        if mode == "append" and i + 1 == die_on:
            writer.arm_die(stage)
        writer.append(C.KIND_MODEL_REQUEST, b"event-%03d" % i, priority=0)
        writer.publish()
        if mode == "roll" and i + 1 == die_on:
            os.environ["BLACKBOX_DIE_AT"] = stage
            writer.roll()
    writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
