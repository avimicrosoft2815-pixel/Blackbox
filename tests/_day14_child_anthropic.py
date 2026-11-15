"""The day-14 child for `anthropic` (used by `tests/test_day14_kill.py`). Not a test module.

Usage: python tests/_day14_child_anthropic.py <data_dir> <base_url> <events_before_die>

The same experiment as `_day14_child.py`, run against the *other* SDK, because "it works for `openai`" is a fact
about `openai`. It is an **unmodified** `anthropic` script: a client is built and `messages.create(stream=True)` is
iterated exactly as the SDK documents. The only lines that know about BlackBox are the four that install the
recorder and the one that kills the process.

Dying on purpose is the point: `SIGKILL` gives no atexit, no flush and no close, so whatever the tape holds
afterwards is whatever the recorder actually made durable while the stream was in flight.
"""
from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# Imported after the path fix, deliberately: this is a child process, not an installed package.
import anthropic

from blackbox import api
from blackbox.cap_sdk.preflight import choose
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink


def main(argv: list[str]) -> int:
    data_dir, base_url, die_after = argv[1], argv[2], int(argv[3])

    # One event per commit, so what is on the tape is exactly what the recorder published before the kill.
    api.install(data_dir=data_dir, publish_ms=0.0)
    with api.recording() as writer:
        if writer is not None:
            writer.batch_bytes = 0
    recorder = SinkRecorder(TapeSink(api.emit))
    decision = choose(recorder)
    print(f"SEAM={decision.seam}", flush=True)

    client = anthropic.Anthropic(api_key="test-key", base_url=base_url, max_retries=0)
    stream = client.messages.create(
        model="mock-1",
        max_tokens=64,
        messages=[{"role": "user", "content": "stream something long"}],
        stream=True,
    )
    seen = 0
    for _event in stream:
        seen += 1
        if seen >= die_after:
            os.kill(os.getpid(), signal.SIGKILL)
    api.uninstall()
    print(f"SURVIVED events={seen}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
