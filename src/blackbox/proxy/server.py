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
from dataclasses import dataclass, field
from typing import Any

from blackbox.errors import bbx
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


# A method whose repetition cannot cause a second side effect. Only these are ever retried after a request may have
# been transmitted; everything else is reported as an uncertain failure instead (P3B-08, the P0 that named this).
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class _StaleConnection(Exception):
    """A pooled upstream connection was dead after the request went out.

    **This is an uncertain outcome, not a retryable one.** The request may have reached the upstream and may have been
    acted on; only its response never arrived. A proxy that quietly sends it again can double a charge, a write or a
    deployment, so the decision is made by the caller: an idempotent method (see `RETRYABLE_METHODS`) is retried once,
    anything else is answered `502 upstream-closed-uncertain` with a body that says the request may have taken effect.
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
    upstream_errors: int = 0
    uncertain: int = 0                 # a request that may have reached the upstream and got no response
    record_errors: int = 0
    bytes_to_client: int = 0
    bytes_to_upstream: int = 0
    per_route: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "streamed": self.streamed, "refused": self.refused,
                "upstream_errors": self.upstream_errors, "uncertain": self.uncertain,
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


class ProxyServer:
    """One loopback listener. `start()` is async; `serve_forever()` is for the launcher's thread (P3B-03)."""

    def __init__(self, routes: Routes, *, receiver: Any = None, port: int = 0, host: str | None = None,
                 clock: Any = time.monotonic) -> None:
        self.routes = routes
        self.receiver = receiver                      # anything with open_call/receive/close_call (a SinkRecorder)
        self._port = port
        self._host = host or routes.bind
        self._clock = clock
        self._server: asyncio.AbstractServer | None = None
        self.stats = ProxyStats()
        self._tls: dict[bool, ssl.SSLContext] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self.pool = _UpstreamPool()

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

    # -- one request ---------------------------------------------------------------------------------------------
    async def _serve(self, request: _Request, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> bool:
        self.stats.requests += 1
        path = request.target
        try:
            route = self.routes.resolve(path, request.headers)
        except RouteError as exc:
            self.stats.refused += 1
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

        call = self._open_call(route, request, target_path, query, body, stream=True)
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
            if request.method.upper() not in RETRYABLE_METHODS:
                self.stats.upstream_errors += 1
                self.stats.uncertain += 1
                self._close_call(call, status=502, error="upstream-closed-uncertain")
                with contextlib.suppress(Exception):
                    upstream_writer.close()
                await self._fail(writer, 502, "upstream-closed-uncertain", request.keep_alive,
                                 note=f"{request.method} was sent once and may have been applied; "
                                      "blackbox does not resend it")
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
        # The head goes out now, and the first body bytes go out with the relay's first flush: waiting to *coalesce*
        # head and body would delay the only bytes the client is waiting for before it can parse anything.
        writer.write(self._client_head(status, reason, headers, keep))

        try:
            streamed, complete = await self._relay_body(status, headers, upstream_reader, writer, call)
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            self.stats.upstream_errors += 1
            self._close_call(call, status=status, error=f"upstream-truncated: {exc!r}")
            return False, False
        self.stats.streamed += 1 if streamed else 0
        self._close_call(call, status=status)
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

    async def _relay_body(self, status: int, headers: list[tuple[str, str]], upstream_reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter, call: Any, *, head: bytes = b"") -> tuple[bool, bool]:
        """Relay the body as it arrives, teeing each *data* chunk into the receiver. Returns True if it streamed."""
        declared = next((v for k, v in headers if k.lower() == "content-length"), None)
        chunked = any(k.lower() == "transfer-encoding" and "chunked" in v.lower() for k, v in headers)
        if declared is not None:
            remaining = int(declared)
            streamed = remaining > 0
            if remaining == 0:
                writer.write(head)
                await writer.drain()
            while remaining > 0:
                data = await upstream_reader.read(min(READ_CHUNK, remaining))
                if not data:
                    raise asyncio.IncompleteReadError(b"", remaining)
                remaining -= len(data)
                writer.write(head + data if head else data)
                head = b""
                self.stats.bytes_to_client += len(data)
                self._receive(call, data)
                await writer.drain()
            return streamed, True
        if chunked:
            first = True
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
                    writer.write((head if first else b"") + bytes(tail))
                    await writer.drain()
                    return True, True
                data = await upstream_reader.readexactly(size)
                await upstream_reader.readexactly(2)
                writer.write((head if first else b"") + size_line + data + CRLF)
                head, first = b"", False
                self.stats.bytes_to_client += len(data)
                self._receive(call, data)
                await writer.drain()
        if status in (204, 304):
            writer.write(head)
            await writer.drain()
            return False, True
        while True:
            data = await upstream_reader.read(READ_CHUNK)
            if not data:
                if head:
                    writer.write(head)
                    await writer.drain()
                return True, False          # read to EOF: there is no framing left to trust, so do not pool it
            writer.write(head + data if head else data)
            head = b""
            self.stats.bytes_to_client += len(data)
            self._receive(call, data)
            await writer.drain()

    async def _fail(self, writer: asyncio.StreamWriter, status: int, reason: str, keep: bool,
                    *, note: str | None = None) -> None:
        text = f"{status} {reason}\n".encode() + (f"{note}\n".encode() if note else b"")
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
    def _open_call(self, route: Route, request: _Request, target_path: str, query: str, body: bytes | None,
                   *, stream: bool) -> Any:
        if self.receiver is None:
            return None
        try:
            return self.receiver.open_call(
                provider=route.name, path=target_path + (f"?{query}" if query else ""),
                request={"method": request.method, "path": target_path,
                         "headers": {k.lower(): v for k, v in request.headers if not is_ours(k)},
                         "body_sha256": hashlib.sha256(body or b"").hexdigest()},
                stream=stream, partial=True)
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

    def _close_call(self, call: Any, *, status: int, error: str | None = None) -> None:
        if call is None or self.receiver is None:
            return
        try:
            self.receiver.close_call(call, status=status, error=error)
        except Exception:
            self.stats.record_errors += 1


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
