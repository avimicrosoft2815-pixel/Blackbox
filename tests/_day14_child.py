"""The day-14 child (used by `tests/test_day14_kill.py`). Not a test module.

Usage: python tests/_day14_child.py <data_dir> <base_url> <events_before_die>

What it is: an **unmodified** `openai` script. It builds a client and streams a completion; the only lines that know
about BlackBox are the four before the client is created (install the recorder, hand it a sink, let the preflight
choose a seam) and the one that kills the process. Nothing is wrapped, nothing is monkeypatched by hand, and the SDK
call looks exactly like the call in the SDK's own README.

Dying on purpose is the point: `SIGKILL` gives no atexit, no flush and no close, so whatever the tape holds
afterwards is whatever the recorder actually made durable while the stream was in flight.
"""
from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# Imported after the path fix, deliberately: this is a child process, not an installed package.
import openai

from blackbox import api
from blackbox.cap_sdk.preflight import choose
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink


def main(argv: list[str]) -> int:
    data_dir, base_url, die_after = argv[1], argv[2], int(argv[3])

    # One event per commit: the gate is about the kill boundary, so the group-commit window is closed and what is on
    # the tape is exactly what the recorder published. A production run keeps the window (and loses at most one
    # group). `publish_ms` is the constructor knob; `batch_bytes` is an attribute, set below.
    api.install(data_dir=data_dir, publish_ms=0.0)
    with api.recording() as writer:
        if writer is not None:
            writer.batch_bytes = 0
    recorder = SinkRecorder(TapeSink(api.emit))
    decision = choose(recorder)
    print(f"SEAM={decision.seam}", flush=True)

    client = openai.OpenAI(api_key="test-key", base_url=base_url, max_retries=0)
    stream = client.chat.completions.create(
        model="mock-1",
        messages=[{"role": "user", "content": "stream something long"}],
        stream=True,
    )
    seen = 0
    for _chunk in stream:
        seen += 1
        if seen >= die_after:
            os.kill(os.getpid(), signal.SIGKILL)
    api.uninstall()
    print(f"SURVIVED events={seen}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
