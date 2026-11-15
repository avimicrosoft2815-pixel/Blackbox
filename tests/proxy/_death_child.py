"""The proxy process the S16 test kills (used by `tests/proxy/test_proxy_death.py`). Not a test module.

Usage: python tests/proxy/_death_child.py <data_dir> <upstream_base_url>

It is a real `ProxyServer` with a real `TapeWriter` behind it, and it announces its port so the parent can drive traffic
through it. Nothing in here knows it is about to die: `SIGKILL` gives no atexit, no flush and no close, which is the
whole point — what the tape holds afterwards is what the recorder actually published while the call was in flight.

The lease is acquired here for the same reason a writer will acquire one: §14.4a's `INTERRUPTED_PROXY` is defined by
"the tape has an open call and the writer's lease is dead", so the test has to be able to answer the second half from
outside the dead process. (Wiring the lease into `TapeWriter` itself belongs to the ticket that owns the writer's
lifetime; this child does exactly what that wiring will do, at the same path.)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from blackbox import api
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer
from blackbox.tape import lease

LEASE_NAME = "writer.lease"


async def main(argv: list[str]) -> int:
    data_dir, upstream = argv[1], argv[2]
    # One publish per emit: the test is about the kill boundary, so the group-commit window is closed and the tape holds
    # what the recorder published rather than what it was still holding.
    api.install(data_dir=data_dir, publish_ms=0.0)
    with api.recording() as writer:
        if writer is not None:
            writer.batch_bytes = 0
    lease.acquire(os.path.join(data_dir, LEASE_NAME))
    recorder = SinkRecorder(TapeSink(api.emit, content="full", coalesce_bytes=1))
    routes = Routes([Route(name="origin", base=upstream, prefix="/p")], default=None)
    server = await ProxyServer(routes, receiver=recorder).start()
    print(json.dumps({"port": server.port, "lease": LEASE_NAME}), flush=True)
    await asyncio.Event().wait()                     # serve until killed; there is no graceful path out of here
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
