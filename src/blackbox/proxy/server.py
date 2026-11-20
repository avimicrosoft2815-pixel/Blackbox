"""P3B-01: the proxy — loopback HTTP/1.1 in, the real provider out, and every byte that comes back on the tape.

This is the universality promise in one file. Any language whose SDK honours a base-URL variable can be recorded
without a line of code changing, because the SDK is pointed at `http://127.0.0.1:<port>` and this process forwards to
the provider exactly as asked. That makes two properties non-negotiable:

* **The passthrough is byte-exact.** A streamed response is relayed as it arrives, and what the client receives is
  sha256-identical to what the upstream sent — no de-chunking the client sees, no re-ordering, no buffering that turns
  a stream into a lump. The tee is on the *data* of each chunk, never on the framing: recording is a tap, and a tap
  that changes the water is not a tap.
* **The recorder can fail and the request still happens.** Every call into the sink is wrapped: a recorder that raises
  or a tape that is full costs frames, never a request. That is the same rule as the in-process seams (D-027), and it
  matters more here because the proxy sits in front of *someone else's* program.

Everything else is HTTP hygiene that a proxy has no licence to skip: hop-by-hop headers are not forwarded in either
direction, `Connection: keep-alive` is honoured with a request loop per connection, `Expect: 100-continue` is answered
before the body is read, both `content-length` and `chunked` request bodies are handled, and an upstream failure is a
`502`/`504` with `x-blackbox-error` naming the class rather than a dropped socket the application cannot explain
(`BBX-E140` not allowed, `BBX-E142` TLS, `BBX-E141` port in use, `BBX-E143` refused bind — the last two in
`routes.py` and the CLI).

Where a destination comes from: **`routes.resolve()`**. This module never builds a host string from a request, a
redirect or a header; the only `open_connection` in the file is handed `route.destination`, which came from the
configured table. That is what lets `tests/test_no_egress.py` assert the proxy's reachable set as a fact.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from blackbox import constants as C
from blackbox.cap_sdk import headers as cap_headers
from blackbox.errors import bbx
from blackbox.proxy import disconnects
from blackbox.proxy.limits import fd_budget, open_fds
from blackbox.proxy.routes import (
    MAX_BODY_BYTES,
    Route,
    RouteError,
    Routes,
    is_ours,
    upstream_not_allowed,
)

CRLF = b"\r\n"
HEADER_LIMIT = 64 * 1024                 # one request's header block; a proxy that reads forever is a memory leak
MAX_LINE = 8 * 1024
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 75.0
READ_CHUNK = 64 * 1024
BACKLOG = 256                          # §15.1 asks for 200 concurrent streams; a backlog of 100 is a refusal
# B3 (docs/RELEASE-BLOCKERS.md): what the proxy does at the ceiling, decided rather than inherited. The recommended
# option there was backpressure, and that is what these numbers are — a request past `MAX_INFLIGHT` keeps its socket and
# *waits* for a slot (nothing the upstream would have served is refused), and a client that opens more sockets than
# `MAX_CONNECTIONS` is answered in milliseconds instead of being left to hang. Neither number is a guess: they are the
# largest pair whose worst case fits the smallest default descriptor budget this project meets (1024 on Linux and macOS)
# even when `limits.py` could not raise anything, Windows included.
MAX_INFLIGHT = 200                     # §15.1's concurrent streams: one slot per request in flight, up and down
MAX_CONNECTIONS = 512                  # clients held open at once (a keep-alive connection costs one descriptor)
QUEUE_WAIT_S = 60.0                    # a request queued longer than this is answered with a 503, never left to stall
# `MAX_CONNECTIONS` client sockets + one upstream per in-flight request + the pool's idle sockets (`2 * routes`) + the
# listener, the tape, and stdio. The assertion below is what `tests/test_proxy_limits.py` holds us to.
WORST_CASE_FDS = MAX_CONNECTIONS + MAX_INFLIGHT
# The listener, the idle pool (at most `2 * routes`), the tape, the sink's log, and stdio, in the worst case this project
# runs into. Counted rather than waved at, because `tests/test_proxy_backpressure.py` adds it to `WORST_CASE_FDS` and
# requires the total to fit below.
PROXY_LOCAL_SLACK_FDS = 96
FD_BUDGET = 1024
# Floors, for the host that refused even `raise_fd_limit()`: a proxy with no concurrency is not a smaller proxy, it is a
# broken one. Below this the caps stop tracking the budget and the explicit `503` is what protects the process.
MIN_INFLIGHT = 8
MIN_CONNECTIONS = 16
WRITE_HIGH_WATER = 64 * 1024           # stop reading upstream while the client is this far behind, so RSS stays flat
WILDCARD_V4 = ".".join(["0"] * 4)      # written this way so the wildcard bind is not a literal in the source
SCHEME = "http"                        # the listener is plain loopback; TLS is upstream's business

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
    "transfer-encoding", "upgrade", "expect", "proxy-connection",
})
# `host` is rewritten by the proxy; `content-length` is recomputed when a body was read; `x-blackbox-*` are ours.

ERROR_HEADER = "x-blackbox-error"
RECORD_ERRORS_HEADER = "x-blackbox-record-errors"

STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 408: "Request Timeout",
    413: "Payload Too Large", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}


# HTTP calls these methods *safe*: the specification says their repetition is not supposed to change the upstream's
# state. That is an intention about well-behaved servers, not a property of the traffic — an analytics endpoint that
# counts a GET, a "mark as read" link, a payment provider's confirmation URL all have effects, and none of them are
# obliged to tell a proxy. So post-transmission retries are **off by default** and this set only ever applies when a
# caller has said, in so many words, that their upstreams are the well-behaved kind (D-032c).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class _Tally:
    """What each side of this one call actually moved, so a disconnect can report two numbers (§14.4a)."""

    to_client: int = 0
    from_upstream: int = 0


class _ClientGone(Exception):
    """The client's socket went away while we were writing to it: §14.4a's `INTERRUPTED_CLIENT`.

    It is its own exception because it was once the *same* exception as an upstream failure — one `except
    (ConnectionError, asyncio.IncompleteReadError)` covered both directions and stamped the client's hang-up
    `upstream-truncated`, which blames the provider for the client's death and sends a reader to the wrong end of the
    call. The proxy sees both ends; it has no excuse for confusing them.
    """

    def __init__(self, tally: _Tally) -> None:
        super().__init__("client went away mid-response")
        self.tally = tally


class _StaleConnection(Exception):
    """A pooled upstream connection was dead after the request went out.

    **This is an uncertain outcome, not a retryable one.** The request may have reached the upstream and may have been
    acted on; only its response never arrived. A proxy that quietly sends it again can double a charge, a write or a
    deployment — and "but it was a GET" is not a defence, because safety is a promise the *upstream* makes, not one the
    method implies. So the default is to answer `502 upstream-closed-uncertain` and say so; a retry happens only when
    the caller turned it on (`retry_transmitted=True`), and then only for `SAFE_METHODS`.
    """


@dataclass
class _Pooled:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    expires_at: float


class _UpstreamPool:
    """Idle upstream connections per route, and the rules that keep them honest.

    Pooling is what makes the §15.1 first-byte budget reachable: a fresh loopback connection costs most of the 0.5 ms
    the row allows, and it is paid on every streamed call. But a pooled connection is a stale connection waiting to
    happen — the origin may have closed it while it sat idle — so the rules are narrow: only a connection whose
    previous response was read to its end is kept, only for `idle_s`, never more than `per_route`, and a request that
    finds a dead one is **retried once on a fresh connection** rather than surfaced as a 502.
    """

    def __init__(self, *, per_route: int = 2, idle_s: float = 20.0) -> None:
        self.per_route = per_route
        self.idle_s = idle_s
        self._idle: dict[str, list[_Pooled]] = {}
        self.reused = 0
        self.retries = 0
        self.discarded = 0                 # found dead *before* the request was written: free, no uncertainty

    def acquire(self, route: Route) -> _Pooled | None:
        stack = self._idle.get(route.name) or []
        now = time.monotonic()
        while stack:
            pooled = stack.pop()
            if pooled.expires_at > now and not pooled.writer.is_closing():
                self.reused += 1
                return pooled
            with contextlib.suppress(Exception):
                pooled.writer.close()
        return None

    def release(self, route: Route, pooled: _Pooled) -> None:
        stack = self._idle.setdefault(route.name, [])
        if len(stack) >= self.per_route:
            with contextlib.suppress(Exception):
                pooled.writer.close()
            return
        pooled.expires_at = time.monotonic() + self.idle_s
        stack.append(pooled)

    async def close_all(self) -> None:
        for stack in self._idle.values():
            for pooled in stack:
                with contextlib.suppress(Exception):
                    pooled.writer.close()
        self._idle.clear()

    def as_dict(self) -> dict[str, Any]:
        return {"reused": self.reused, "retries": self.retries, "discarded": self.discarded,
                "idle": {name: len(stack) for name, stack in sorted(self._idle.items())}}


@dataclass
class ProxyStats:
    """What the proxy did, for `SUPPORTED.md` and the bench. Counts, never content."""

    requests: int = 0
    streamed: int = 0
    refused: int = 0
    queued: int = 0                    # requests that waited for a slot: backpressure, not a failure (B3)
    max_queue_wait_ms: float = 0.0
    refused_reasons: dict[str, int] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)   # the ceilings in force, and where they came from
    spans: int = 0                     # proxy-owned spans opened: one per forwarded request (B4)
    spans_capped: int = 0              # requests the reserved slot band could not cover (never silently dropped)
    upstream_errors: int = 0
    uncertain: int = 0                 # a request that may have reached the upstream and got no response
    client_gone: int = 0               # the client hung up while the upstream was still sending (§14.4a)
    upstream_gone: int = 0             # the upstream closed before a terminal event, client still listening (§14.4a)
    record_errors: int = 0
    bytes_to_client: int = 0
    bytes_to_upstream: int = 0
    per_route: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        """Count a request the proxy did not pass through, under the reason a user could act on."""
        self.refused += 1
        self.refused_reasons[reason] = self.refused_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "streamed": self.streamed, "refused": self.refused,
                "refused_reasons": dict(sorted(self.refused_reasons.items())),
                "queued": self.queued, "max_queue_wait_ms": round(self.max_queue_wait_ms, 1),
                "caps": dict(self.caps), "spans": self.spans, "spans_capped": self.spans_capped,
                "upstream_errors": self.upstream_errors, "uncertain": self.uncertain,
                "client_gone": self.client_gone, "upstream_gone": self.upstream_gone,
                "record_errors": self.record_errors,
                "bytes_to_client": self.bytes_to_client, "bytes_to_upstream": self.bytes_to_upstream,
                "per_route": dict(sorted(self.per_route.items()))}


@dataclass
class _Request:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None

    @property
    def keep_alive(self) -> bool:
        connection = (self.header("connection") or "").lower()
        if self.version == "HTTP/1.0":
            return "keep-alive" in connection
        return "close" not in connection


@dataclass(frozen=True)
class Caps:
    """The two ceilings this listener uses, and the sentence that says where they came from."""

    max_inflight: int
    max_connections: int
    budget: int | None
    reason: str
    in_use: int | None = None       # descriptors already held when the plan was made, where the platform can count them


def plan_caps(budget: int | None, in_use: int | None = None) -> Caps:
    """Derive the ceilings from the descriptor budget the process actually has *now*.

    The documented pair (`MAX_INFLIGHT` 200, `MAX_CONNECTIONS` 512) is what a host with 1024 descriptors can hold
    (`512 + 200 + 96 = 808`). A host that refused `raise_fd_limit()` — a macOS shell starting at 256, a container with a
    hard limit nobody can pass — must not be handed that pair: the caps shrink to fit what it has, and the reason says so.

    `in_use` is what the process was **already** holding when the listener was planned (the tape, the child's own files,
    the SDK's sockets, the interpreter). Those descriptors come out of the same budget, so leaving them out would plan
    the slack twice: `room = budget - slack - in_use`. It is a lower bound (`open_fds()` counts with one descriptor open
    for the scan), which is the direction that errs safe.

    On Windows there is no readable budget (`fd_budget()` is `None`), so the documented pair stands as a *policy* number
    and the `503` past it is what bounds the host; the reason says that too, rather than implying arithmetic that does not
    exist there.
    """
    if budget is None:
        return Caps(MAX_INFLIGHT, MAX_CONNECTIONS, None,
                    f"this platform publishes no descriptor budget, so {MAX_CONNECTIONS} connections and "
                    f"{MAX_INFLIGHT} in flight are policy numbers; the 503 at the ceiling is what bounds the process",
                    in_use)
    held = max(0, int(in_use or 0))
    room = budget - PROXY_LOCAL_SLACK_FDS - held
    held_note = f" and {held} are already open" if held else ""
    if room >= MAX_CONNECTIONS + MAX_INFLIGHT:
        return Caps(MAX_INFLIGHT, MAX_CONNECTIONS, budget,
                    f"{MAX_CONNECTIONS} connections + {MAX_INFLIGHT} in flight + {PROXY_LOCAL_SLACK_FDS} for the process "
                    f"fits the {budget} descriptors this host allows" + (f", of which {held} are already open" if held else ""),
                    in_use)
    connections = min(MAX_CONNECTIONS, max(MIN_CONNECTIONS, room * 3 // 5))
    inflight = min(MAX_INFLIGHT, max(MIN_INFLIGHT, room - connections))
    if room < MIN_CONNECTIONS + MIN_INFLIGHT:
        return Caps(inflight, connections, budget,
                    f"this host allows only {budget} descriptors{held_note}, below the "
                    f"{PROXY_LOCAL_SLACK_FDS + MIN_CONNECTIONS + MIN_INFLIGHT + held} the proxy needs: the caps are the "
                    f"floors ({connections} connections, {inflight} in flight) and the 503 at them is what protects the "
                    f"process", in_use)
    return Caps(inflight, connections, budget,
                f"this host allows {budget} descriptors{held_note}, so the caps are {connections} connections + "
                f"{inflight} in flight + {PROXY_LOCAL_SLACK_FDS} for the process", in_use)


class ProxyServer:
    """One loopback listener. `start()` is async; `serve_forever()` is for the launcher's thread (P3B-03)."""

    def __init__(self, routes: Routes, *, receiver: Any = None, port: int = 0, host: str | None = None,
                 retry_transmitted: bool = False, clock: Any = time.monotonic,
                 max_inflight: int | None = None, max_connections: int | None = None,
                 queue_wait_s: float = QUEUE_WAIT_S) -> None:
        self.routes = routes
        self.receiver = receiver                      # anything with open_call/receive/close_call (a SinkRecorder)
        # Off by default: see `SAFE_METHODS`. A caller who turns this on is asserting something about their upstreams.
        self.retry_transmitted = retry_transmitted
        self._port = port
        self._host = host or routes.bind
        self._clock = clock
        self._server: asyncio.AbstractServer | None = None
        self.stats = ProxyStats()
        # The numbers are parameters so a test can be a *real* burst at a *real* ceiling without 512 sockets: the
        # behaviour at the limit is the thing under test, and it does not depend on the size of the limit. Left unset,
        # they are *derived* from this host's descriptor budget — a plan, not a constant a smaller host cannot honour.
        # `open_fds()` before the listener exists: the plan must not spend descriptors the process already holds.
        self.caps = plan_caps(fd_budget(), in_use=open_fds())
        self.max_inflight = max(1, int(max_inflight if max_inflight is not None else self.caps.max_inflight))
        self.max_connections = max(1, int(max_connections if max_connections is not None else self.caps.max_connections))
        self.queue_wait_s = max(0.0, float(queue_wait_s))
        self.stats.caps = {"max_inflight": self.max_inflight, "max_connections": self.max_connections,
                           "fd_budget": self.caps.budget, "open_at_start": self.caps.in_use,
                           "why": self.caps.reason}
        self._inflight = asyncio.Semaphore(self.max_inflight)
        # B4: the proxy opens a span of its own for every request it forwards, so a node-to-node edge has two *recorded*
        # ends instead of one recorded end and one inferred one. Slots come from the band `constants` reserves for this
        # writer, never from the hook folder's, so two writers on one tape cannot merge two spans into one node.
        self._span_slot = int(C.NODE_SLOT_PROXY_BASE)
        self._tls: dict[bool, ssl.SSLContext] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self.pool = _UpstreamPool()
        # Called, once, when this listener is closed: the supervisor's early notice (defect 58). A watchdog can only
        # notice on a tick, and a tick is most of a 100 ms budget; a close knows the moment it happens, so it says so.
        # It is a *notification*, not a promise: a listener that dies for another reason still waits for the tick.
        self.lost_cb: Callable[[], None] | None = None

    # -- lifecycle -----------------------------------------------------------------------------------------------
    async def start(self) -> ProxyServer:
        try:
            self._server = await asyncio.start_server(self._handle, self._host, self._port, limit=MAX_LINE,
                                                     backlog=BACKLOG)
        except OSError as exc:
            raise bbx("BBX-E141", f"{self._host}:{self._port} — {exc}") from exc
        sockets = self._server.sockets or ()
        if sockets:
            self._port = sockets[0].getsockname()[1]
        return self

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_serving(self) -> bool:
        """True while the listener accepts connections — what the supervisor's watchdog watches."""
        return self._server is not None and self._server.is_serving()

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self._host in (WILDCARD_V4, "::") else self._host
        return f"{SCHEME}://{host}:{self._port}"

    async def close(self) -> None:
        """Stop listening and hang up on anything still connected: a `close()` that waits out a keep-alive idle
        timeout is a shutdown that takes five minutes."""
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()
        await self.pool.close_all()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
            notice, self.lost_cb = self.lost_cb, None          # one-shot: a second close is not a second loss
            if notice is not None:
                with contextlib.suppress(Exception):
                    notice()

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def __aenter__(self) -> ProxyServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- one connection ------------------------------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        _nodelay(writer)
        try:
            if len(self._writers) > self.max_connections:
                # B3's hard ceiling. Past `max_connections` there is no answer that keeps the promise — the recorder
                # cannot hold the socket — so it answers *now*, with a 503 and a reason, which a client can retry on.
                # The alternative measured before this was a child waiting four seconds for a connection nothing would
                # ever complete, and asyncio logging a traceback per attempt at `accept`.
                self.stats.refuse("connection-limit")
                await self._answer_at_limit(reader, writer)
                return
            while True:
                try:
                    request = await asyncio.wait_for(self._read_request_head(reader), timeout=IDLE_TIMEOUT)
                except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                    return
                if request is None:
                    return
                keep = await self._serve(request, reader, writer)
                if not keep:
                    return
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _read_request_head(self, reader: asyncio.StreamReader) -> _Request | None:
        try:
            line = await reader.readuntil(CRLF)
        except asyncio.IncompleteReadError:
            return None
        line = line.rstrip(CRLF)
        if not line:
            return None
        parts = line.decode("latin-1").split(" ")
        if len(parts) != 3:
            raise upstream_not_allowed(f"malformed request line {line[:80]!r}")
        method, target, version = parts
        headers: list[tuple[str, str]] = []
        total = 0
        while True:
            raw = await reader.readuntil(CRLF)
            total += len(raw)
            if total > HEADER_LIMIT:
                raise upstream_not_allowed("header block over limit")
            if raw in (CRLF, b"\n"):
                break
            name, _, value = raw.rstrip(CRLF).decode("latin-1").partition(":")
            headers.append((name.strip(), value.strip()))
        return _Request(method=method, target=target, version=version, headers=headers)

    async def _answer_at_limit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer a connection that cannot be held, cheaply and in well-formed HTTP.

        The request head is read first *if it is already there*: a client that has sent its request deserves a `503` it
        can parse, and a client that connected and said nothing costs two seconds and a descriptor, not a slot.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._read_request_head(reader), timeout=2.0)
        await self._fail(writer, 503,
                         f"connection-limit: the recorder already holds its {self.max_connections} connections",
                         keep=False,
                         hint="this is the recorder's ceiling, not the provider's: retry when its in-flight requests "
                              "finish, or run fewer at once")

    # -- one request ---------------------------------------------------------------------------------------------
    async def _serve(self, request: _Request, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> bool:
        self.stats.requests += 1
        started = self._clock()
        try:
            await asyncio.wait_for(self._inflight.acquire(), timeout=self.queue_wait_s)
        except TimeoutError:
            # A bounded wait is the whole point: an unbounded one is the silent stall this blocker is about.
            self.stats.refuse("queue-timeout")
            self.stats.max_queue_wait_ms = max(self.stats.max_queue_wait_ms, self.queue_wait_s * 1000.0)
            await self._fail(writer, 503,
                             f"queue-timeout: no slot within {self.queue_wait_s:.0f}s "
                             f"(the recorder already has {self.max_inflight} requests in flight)", request.keep_alive,
                             hint="the requests already in flight are slower than the recorder's queue allowance: retry, "
                                  "or run fewer at once")
            return request.keep_alive
        waited_ms = (self._clock() - started) * 1000.0
        self.stats.max_queue_wait_ms = max(self.stats.max_queue_wait_ms, waited_ms)
        if waited_ms >= 1.0:
            self.stats.queued += 1
        try:
            return await self._serve_request(request, reader, writer)
        finally:
            self._inflight.release()

    async def _serve_request(self, request: _Request, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> bool:
        path = request.target
        try:
            route = self.routes.resolve(path, request.headers)
        except RouteError as exc:
            self.stats.refuse(exc.reason)
            await self._fail(writer, exc.status or 502, exc.reason, request.keep_alive)
            return request.keep_alive

        target_path = route.target_path(path.split("?", 1)[0])
        query = path.split("?", 1)[1] if "?" in path else ""
        self.stats.per_route[route.name] = self.stats.per_route.get(route.name, 0) + 1

        # `Expect: 100-continue` is answered *before* the body is read: a client that honours it is waiting for this
        # line, so reading first is a deadlock with extra steps.
        if (request.header("expect") or "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue" + CRLF + CRLF)
            await writer.drain()
        # Connect to the upstream *before* reading the request body: a TCP handshake and a body read are independent
        # work, and doing them in series is the difference between the §15.1 first-byte budget and missing it.
        connecting = asyncio.ensure_future(self._connect(route))
        body = await self._read_body(request, reader)
        if body is None:
            connecting.cancel()
            return False

        call_id = cap_headers.parse_call(request.header(cap_headers.CALL_HEADER))
        span = self._open_span(route, request, target_path, call_id=call_id)
        call = self._open_call(route, request, target_path, query, body, stream=True, node_slot=span)
        if call is None and span is not None:
            self._close_span(span, outcome="not-recorded", status=0)
        try:
            upstream_reader, upstream_writer, reused = await _resolve_future(connecting)
        except RouteError as exc:
            self.stats.upstream_errors += 1
            self._close_call(call, status=exc.status, error=exc.reason)
            await self._fail(writer, exc.status, exc.reason, request.keep_alive)
            return request.keep_alive
        except (TimeoutError, OSError) as exc:
            self.stats.upstream_errors += 1
            reason = "upstream-timeout" if isinstance(exc, TimeoutError) else "upstream-unreachable"
            self._close_call(call, status=504, error=reason)
            await self._fail(writer, 504, reason, request.keep_alive)
            return request.keep_alive

        keep, reusable = request.keep_alive, False
        try:
            keep, reusable = await self._exchange(request, route, target_path, query, body, upstream_reader,
                                                  upstream_writer, writer, call, reused=reused)
        except _StaleConnection:
            # The request went out and no response head came back. It may have been acted on, so a retry is only
            # defensible for a method whose repetition cannot have a second side effect.
            if not (self.retry_transmitted and request.method.upper() in SAFE_METHODS):
                self.stats.upstream_errors += 1
                self.stats.uncertain += 1
                self._close_call(call, status=502, error="upstream-closed-uncertain")
                with contextlib.suppress(Exception):
                    upstream_writer.close()
                why = ("post-transmission retries are off by default because a repeated request can have a second "
                       "effect; --proxy-retry-transmitted opts in for GET/HEAD/OPTIONS")
                if self.retry_transmitted:
                    why = f"{request.method} is not one of GET/HEAD/OPTIONS, so it is never resent"
                await self._fail(writer, 502, "upstream-closed-uncertain", request.keep_alive,
                                 note=f"{request.method} was sent once and may have been applied; "
                                      f"blackbox did not resend it — {why}")
                return request.keep_alive
            self.pool.retries += 1
            with contextlib.suppress(Exception):
                upstream_writer.close()
            try:
                upstream_reader, upstream_writer = await self._connect_fresh(route)
            except (TimeoutError, OSError):
                self.stats.upstream_errors += 1
                self._close_call(call, status=504, error="upstream-unreachable")
                await self._fail(writer, 504, "upstream-unreachable", request.keep_alive)
                return request.keep_alive
            keep, reusable = await self._exchange(request, route, target_path, query, body, upstream_reader,
                                                  upstream_writer, writer, call, reused=False)
        if reusable and not upstream_writer.is_closing():
            self.pool.release(route, _Pooled(upstream_reader, upstream_writer, 0.0))
        else:
            with contextlib.suppress(Exception):
                upstream_writer.close()
        return keep

    async def _exchange(self, request: _Request, route: Route, target_path: str, query: str, body: bytes | None,
                        upstream_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter,
                        writer: asyncio.StreamWriter, call: Any, *, reused: bool = False) -> tuple[bool, bool]:
        out = self._upstream_head(request, route, target_path, query, body)
        try:
            upstream_writer.write(out)
            if body:
                upstream_writer.write(body)
                self.stats.bytes_to_upstream += len(body)
            await upstream_writer.drain()
        except (ConnectionError, OSError):
            # The request was written into a socket whose peer is gone. Whether any of it was *received* is exactly
            # the thing a proxy cannot know, so it takes the same path as a missing response head.
            if reused:
                raise _StaleConnection from None
            self.stats.upstream_errors += 1
            self._close_call(call, status=502, error="upstream-closed")
            await self._fail(writer, 502, "upstream-closed", request.keep_alive)
            return request.keep_alive, False

        try:
            status, reason, headers, upstream_version = await self._read_response_head(upstream_reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            if reused:
                # An idle pooled connection the origin had already closed. The client has not been written to yet.
                raise _StaleConnection from None
            self.stats.upstream_errors += 1
            self._close_call(call, status=502, error="upstream-closed")
            await self._fail(writer, 502, "upstream-closed", request.keep_alive)
            return request.keep_alive, False

        connection_hdr = " ".join(v.lower() for k, v in headers if k.lower() == "connection")
        upstream_keeps = ("close" not in connection_hdr and upstream_version != "HTTP/1.0") or \
                         (upstream_version == "HTTP/1.0" and "keep-alive" in connection_hdr)
        keep = request.keep_alive and upstream_keeps
        tally = _Tally()
        try:
            # The head goes out now, and the first body bytes go out with the relay's first flush: waiting to *coalesce*
            # head and body would delay the only bytes the client is waiting for before it can parse anything.
            self._to_client(writer, self._client_head(status, reason, headers, keep), tally)
            streamed, complete = await self._relay_body(status, headers, upstream_reader, writer, call, tally=tally)
        except _ClientGone as gone:
            # The app died or cancelled and the model kept talking: the tokens it produced are a cost nobody received,
            # which is the single most useful thing this record can say (§14.4a). The upstream connection is not pooled
            # — there is a half-read body on it — and the client socket is already gone.
            self.stats.client_gone += 1
            self._close_call(call, status=0, error=disconnects.classify(
                closed_first="client", status=0, client_received_bytes=gone.tally.to_client,
                proxy_received_bytes=gone.tally.from_upstream).as_error(),
                partial=True, ending="unverified",
                end_reason="the client disconnected while the body was still arriving")
            return False, False
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            self.stats.upstream_errors += 1
            self.stats.upstream_gone += 1
            self._close_call(call, status=status, error=disconnects.classify(
                closed_first="upstream", status=status, client_received_bytes=tally.to_client,
                proxy_received_bytes=tally.from_upstream).as_error(),
                partial=True, ending="unverified",
                end_reason="the upstream connection ended before the body's own framing did")
            return False, False
        self.stats.streamed += 1 if streamed else 0
        if complete:
            self._close_call(call, status=status, partial=False, ending="terminated")
        else:
            # A body with no `content-length` and no chunked framing ends when the connection does. That is a legitimate
            # HTTP/1.0 shape, and it is also what a cut looks like: nothing in the bytes says which happened, so the
            # frame says `unverified` and the reason names the ambiguity instead of resolving it.
            self._close_call(call, status=status, partial=True, ending="unverified",
                             end_reason="the body ended at the connection's close: no length, no chunked terminator, "
                                        "so whether it was complete is not recorded")
        return keep, upstream_keeps and complete

    # -- plumbing ------------------------------------------------------------------------------------------------
    async def _read_response_head(self, reader: asyncio.StreamReader
                                  ) -> tuple[int, str, list[tuple[str, str]], str]:
        line = (await reader.readuntil(CRLF)).rstrip(CRLF).decode("latin-1")
        parts = line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise asyncio.IncompleteReadError(line.encode("latin-1", "replace"), None)
        status = int(parts[1])
        version = parts[0]
        reason = parts[2] if len(parts) > 2 else STATUS_TEXT.get(status, "")
        headers: list[tuple[str, str]] = []
        total = 0
        while True:
            raw = await reader.readuntil(CRLF)
            total += len(raw)
            if total > HEADER_LIMIT:
                raise asyncio.IncompleteReadError(raw, None)
            if raw in (CRLF, b"\n"):
                return status, reason, headers, version
            name, _, value = raw.rstrip(CRLF).decode("latin-1").partition(":")
            headers.append((name.strip(), value.strip()))

    async def _read_body(self, request: _Request, reader: asyncio.StreamReader) -> bytes | None:
        """`content-length` or `chunked`, both bounded. `None` means the body was malformed and 400 was written."""
        if request.method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return b""
        length = request.header("content-length")
        if length is not None:
            try:
                wanted = int(length)
            except ValueError:
                return b""
            if wanted <= 0:
                return b""
            if wanted > MAX_BODY_BYTES:
                raise upstream_not_allowed(f"request body of {wanted} bytes is over the proxy limit")
            data = await reader.readexactly(wanted)
            self.stats.bytes_to_upstream += len(data)
            return data
        if (request.header("transfer-encoding") or "").lower().find("chunked") >= 0:
            return await self._read_chunked(reader)
        return b""

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        out = bytearray()
        while True:
            line = (await reader.readuntil(CRLF)).rstrip(CRLF)
            size = int(line.split(b";")[0] or b"0", 16)
            if size == 0:
                while True:                                  # trailers, then the blank line
                    trailer = await reader.readuntil(CRLF)
                    if trailer in (CRLF, b"\n"):
                        break
                break
            out += await reader.readexactly(size)
            await reader.readexactly(2)
            if len(out) > MAX_BODY_BYTES:
                raise upstream_not_allowed("chunked request body over the proxy limit")
        return bytes(out)

    async def _connect(self, route: Route) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bool]:
        """A pooled connection if the route has one that is *still* open, otherwise a fresh one. `bool` says which.

        The liveness check happens here, **before any byte of the request is written**, because that is the only point
        at which discarding a connection is free of consequences. A pooled connection that passes this check can still
        be closed a microsecond later; that case is `_StaleConnection`, and it is not free.
        """
        while True:
            pooled = self.pool.acquire(route)
            if pooled is None:
                break
            if pooled.writer.is_closing() or pooled.reader.at_eof() or pooled.reader.exception() is not None:
                self.pool.discarded += 1
                with contextlib.suppress(Exception):
                    pooled.writer.close()
                continue
            return pooled.reader, pooled.writer, True
        reader, writer = await self._connect_fresh(route)
        return reader, writer, False

    async def _connect_fresh(self, route: Route) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        context: ssl.SSLContext | None = None
        if route.scheme == "https":
            context = self._context(verify=route.verify_tls)
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(*route.destination, ssl=context,
                                        server_hostname=route.host if context else None),
                timeout=CONNECT_TIMEOUT)
            _nodelay(upstream_writer)
            return upstream_reader, upstream_writer
        except ssl.SSLCertVerificationError as exc:
            raise RouteError("BBX-E142", f"{route.name}: {exc}", reason="upstream-tls", status=502) from exc

    def _context(self, *, verify: bool) -> ssl.SSLContext:
        """One context per verification setting, built once: `create_default_context()` is not free."""
        cached = self._tls.get(verify)
        if cached is not None:
            return cached
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._tls[verify] = context
        return context

    def _upstream_head(self, request: _Request, route: Route, target_path: str, query: str,
                       body: bytes | None) -> bytes:
        target = target_path + (f"?{query}" if query else "")
        lines = [f"{request.method} {target} HTTP/1.1"]
        for name, value in request.headers:
            lowered = name.lower()
            if lowered in HOP_BY_HOP or is_ours(name):
                continue
            if lowered == "content-length":
                continue
            lines.append(f"{name}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        elif request.method in ("POST", "PUT", "PATCH"):
            lines.append("Content-Length: 0")
        lines.append(f"Host: {route.host}" + ("" if route.port in (80, 443) else f":{route.port}"))
        lines.append("Connection: keep-alive")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _client_head(self, status: int, reason: str, headers: list[tuple[str, str]], keep: bool) -> bytes:
        lines = [f"HTTP/1.1 {status} {reason or STATUS_TEXT.get(status, 'Unknown')}"]
        for name, value in headers:
            if name.lower() in HOP_BY_HOP or is_ours(name) or name.lower() == "content-length":
                continue
            lines.append(f"{name}: {value}")
        declared = next((v for k, v in headers if k.lower() == "content-length"), None)
        if declared is not None:
            lines.append(f"Content-Length: {declared}")
        else:
            lines.append("Transfer-Encoding: chunked")
        lines.append("Connection: keep-alive" if keep else "Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _to_client(self, writer: asyncio.StreamWriter, data: bytes, tally: _Tally) -> None:
        """The only place the proxy writes to a client, so the only place a dead client has to be named correctly.

        It does not count bytes: the caller counts the *payload* it is writing (a chunk's data, not its framing), so the
        two numbers in a disconnect record are comparable with the byte count an SDK tape keeps — body against body.
        """
        if writer.is_closing() or getattr(writer.transport, "is_closing", lambda: False)():
            raise _ClientGone(tally)
        try:
            writer.write(data)
        except (ConnectionError, OSError):
            raise _ClientGone(tally) from None

    async def _flush(self, writer: asyncio.StreamWriter, tally: _Tally) -> None:
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            raise _ClientGone(tally) from None

    async def _relay_body(self, status: int, headers: list[tuple[str, str]], upstream_reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter, call: Any, *, tally: _Tally,
                          head: bytes = b"") -> tuple[bool, bool]:
        """Relay the body as it arrives, teeing each *data* chunk into the receiver. Returns True if it streamed."""
        declared = next((v for k, v in headers if k.lower() == "content-length"), None)
        chunked = any(k.lower() == "transfer-encoding" and "chunked" in v.lower() for k, v in headers)
        if declared is not None:
            remaining = int(declared)
            streamed = remaining > 0
            if remaining == 0:
                self._to_client(writer, head, tally)
                await self._flush(writer, tally)
            while remaining > 0:
                data = await upstream_reader.read(min(READ_CHUNK, remaining))
                if not data:
                    raise asyncio.IncompleteReadError(b"", remaining)
                remaining -= len(data)
                tally.from_upstream += len(data)
                tally.to_client += len(data)
                self._to_client(writer, head + data if head else data, tally)
                head = b""
                self.stats.bytes_to_client += len(data)
                self._receive(call, data)
                await self._flush(writer, tally)
            return streamed, True
        if chunked:
            first = True
            unwritten = 0                  # bytes handed to the transport since the last drain
            while True:
                size_line = await upstream_reader.readuntil(CRLF)
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    tail = bytearray(size_line)
                    while True:
                        line = await upstream_reader.readuntil(CRLF)
                        tail += line
                        if line in (CRLF, b"\n"):
                            break
                    self._to_client(writer, (head if first else b"") + bytes(tail), tally)
                    await self._flush(writer, tally)
                    return True, True
                data = await upstream_reader.readexactly(size)
                await upstream_reader.readexactly(2)
                tally.from_upstream += len(data)
                tally.to_client += len(data)
                self._to_client(writer, (head if first else b"") + size_line + data + CRLF, tally)
                head, first = b"", False
                self.stats.bytes_to_client += len(data)
                self._receive(call, data)
                # Drain under backpressure, not after every chunk: `drain()` yields to every other task, and a stream
                # of 60-byte chunks would pay that once per chunk on a loopback path that never fills the buffer.
                unwritten += len(data)
                if unwritten >= WRITE_HIGH_WATER or writer.transport.get_write_buffer_size() > WRITE_HIGH_WATER:
                    await self._flush(writer, tally)
                    unwritten = 0
        if status in (204, 304):
            self._to_client(writer, head, tally)
            await self._flush(writer, tally)
            return False, True
        while True:
            data = await upstream_reader.read(READ_CHUNK)
            if not data:
                if head:
                    self._to_client(writer, head, tally)
                    await self._flush(writer, tally)
                return True, False          # read to EOF: there is no framing left to trust, so do not pool it
            tally.from_upstream += len(data)
            tally.to_client += len(data)
            self._to_client(writer, head + data if head else data, tally)
            head = b""
            self.stats.bytes_to_client += len(data)
            self._receive(call, data)
            await self._flush(writer, tally)

    async def _fail(self, writer: asyncio.StreamWriter, status: int, reason: str, keep: bool,
                    *, note: str | None = None, hint: str | None = None) -> None:
        """Answer with a status, a machine-readable reason in `x-blackbox-error`, and — optionally — a sentence for the
        developer reading the failed request. `hint` is not a header: it is the body line that says what to do, which is
        the difference between a recorder that refuses a request and one that refuses it silently."""
        text = (f"{status} {reason}\n".encode() + (f"{hint}\n".encode() if hint else b"")
                + (f"{note}\n".encode() if note else b""))
        head = [
            f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'Error')}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(text)}",
            f"{ERROR_HEADER}: {reason}",
            *([f"{ERROR_HEADER}-detail: uncertain"] if note else []),
            "Connection: keep-alive" if keep else "Connection: close",
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + text)
        with contextlib.suppress(Exception):
            await writer.drain()

    # -- recording (fail-open, always) ---------------------------------------------------------------------------
    def _open_span(self, route: Route, request: _Request, target_path: str, *,
                   call_id: str | None) -> int | None:
        """Open this writer's own span for one forwarded request, and return its slot (or `None`).

        This is the half of B4 that had to exist before any node-to-node edge could be honest: the app's tape records a
        node because its own writer opened one, and the proxy's tape records a node because *it* opened one. The call id
        both sides wrote is then the evidence joining two recorded spans rather than one span and a guess. Nothing here
        describes the app's node: `owner: "proxy"` is on every span this opens.
        """
        receiver = self.receiver
        if receiver is None or not hasattr(receiver, "span_open"):
            return None
        if self._span_slot > C.NODE_SLOT_PROXY_MAX:
            self.stats.spans_capped += 1
            return None
        slot = self._span_slot
        self._span_slot += 1
        try:
            recorded = receiver.span_open(slot, name=f"{request.method} {target_path}", kind="proxy_call",
                                          owner="proxy", route=route.name, call_id=call_id,
                                          linked_run=cap_headers.parse(request.header(cap_headers.HEADER))[0])
        except Exception:
            self.stats.record_errors += 1
            return None
        if recorded is False:
            # A receiver with no sink behind it: nothing will be written, so the call must not be given a slot the
            # graph would then treat as a recorded span.
            return None
        self.stats.spans += 1
        return slot

    def _close_span(self, slot: int, *, outcome: str, status: int, error: str | None = None) -> None:
        receiver = self.receiver
        if receiver is None or not hasattr(receiver, "span_close"):
            return
        try:
            receiver.span_close(slot, outcome=outcome, status=status, error=error)
        except Exception:
            self.stats.record_errors += 1

    def _open_call(self, route: Route, request: _Request, target_path: str, query: str, body: bytes | None,
                   *, stream: bool, node_slot: int | None = None) -> Any:
        if self.receiver is None:
            return None
        # `x-blackbox-run` is the one header the proxy *reads*: it names the run this call belongs to, which is how an
        # exact link is made across a process boundary. It is stripped before the upstream like every other ours.
        linked_run, _node = cap_headers.parse(request.header(cap_headers.HEADER))
        # D-053: the per-call id the SDK sent, recorded on the proxy's own frame for the same request. Both sides then
        # carry one id for one call, which is the only thing that can justify a node-to-call edge.
        call_id = cap_headers.parse_call(request.header(cap_headers.CALL_HEADER))
        try:
            return self.receiver.open_call(
                provider=route.name, path=target_path + (f"?{query}" if query else ""),
                request={"method": request.method, "path": target_path,
                         "headers": {k.lower(): v for k, v in request.headers if not is_ours(k)},
                         "body_sha256": hashlib.sha256(body or b"").hexdigest()},
                stream=stream, partial=False, linked_run=linked_run, call_id=call_id, node_slot=node_slot)
        except Exception:
            self.stats.record_errors += 1
            return None

    def _receive(self, call: Any, data: bytes) -> None:
        if call is None or self.receiver is None:
            return
        try:
            self.receiver.receive(call, data)
        except Exception:
            self.stats.record_errors += 1

    def _close_call(self, call: Any, *, status: int, error: str | None = None, partial: bool = False,
                    ending: str | None = None, end_reason: str | None = None) -> None:
        """Close a proxied call, saying what this writer could see about the body's ending.

        The proxy reads the response's framing itself, so it has an answer a seam cannot always give: a body that ended
        inside its `content-length`, or at a chunked terminator, *finished where the provider said it would*
        (`partial: False`, `ending: "terminated"`). Before this, every proxied call was recorded `partial: True`, and
        `diagnose` reported "the response stream ended before the provider finished it" for a 404 that arrived complete
        (defect 77).
        """
        if call is None or self.receiver is None:
            return
        try:
            self.receiver.close_call(call, status=status, error=error, partial=partial, ending=ending,
                                     end_reason=end_reason)
        except Exception:
            self.stats.record_errors += 1
        slot = getattr(call, "node_slot", None)
        if isinstance(slot, int) and slot:
            self._close_span(slot, outcome="error" if error else "ok", status=status, error=error)


async def _resolve_future(future: asyncio.Future[Any]) -> Any:
    """Await a connect that was started earlier. Its exception must surface here, not as an unretrieved warning."""
    try:
        return await future
    except asyncio.CancelledError:
        raise
    except BaseException:
        raise


def _nodelay(writer: asyncio.StreamWriter) -> None:
    """`TCP_NODELAY` on both sides.

    Without it, a response head written and drained separately from its first body bytes waits on the peer's delayed
    ACK — which is exactly the first-byte overhead §15.1 budgets at 0.5 ms, and the first version of the bench measured
    0.795 ms because of it. A proxy may not add a fifth of a millisecond to a stream it does not otherwise touch.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


__all__ = ["ERROR_HEADER", "HOP_BY_HOP", "RECORD_ERRORS_HEADER", "ProxyServer", "ProxyStats"]
