"""A local mock provider (P3-13): OpenAI-style and Anthropic-style endpoints, over stdlib HTTP, on loopback only.

Why this exists: the day-14 rule is a *streaming* claim — an in-flight model call has to survive `kill -9` with the
right `chunks_received` and `bytes_received`. That cannot be tested against a real provider (no keys here, and a
test that needs the internet is not a gate), and it cannot be tested with a fake client either, because the thing
under test is the byte path: an HTTP request goes out, the response arrives in chunks over time, and the recorder
tees what the socket actually delivered.

So: a real HTTP server on 127.0.0.1, speaking just enough of both dialects to be indistinguishable *to the SDK* for
the paths that are tested, with knobs for the things that break recorders — chunk boundaries, an inter-chunk delay, a
truncated stream, a slow first byte, an error mid-stream, and a 429.

It is deliberately not a general provider emulator. `SUPPORTED.md` and `docs/compatibility.md` say what it covers,
and nothing here is evidence about a real provider.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Scenario:
    """What the mock should do for the next request. Every field has a boring default."""

    model: str = "mock-1"
    chunks: int = 5
    chunk_text: str = "token "
    delay_ms: float = 0.0              # between chunks: the window a kill has to land in
    first_byte_delay_ms: float = 0.0
    truncate_after: int | None = None  # close the socket after N chunks: a killed stream, from the client's view
    error_after: int | None = None     # emit an SSE error event after N chunks
    status: int = 200
    body_bytes: int = 0                # extra bytes appended to each chunk (a payload-size knob)
    tool_call: dict[str, Any] | None = None
    requests: int = field(default=0, init=False)


class MockProvider:
    """A loopback HTTP server. `base_url` is what an SDK is pointed at."""

    def __init__(self, scenario: Scenario | None = None) -> None:
        self.scenario = scenario or Scenario()
        self.received: list[dict[str, Any]] = []
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the mock provider is not running")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def openai_base_url(self) -> str:
        """`OpenAI(base_url=...)` wants the version prefix; the mock serves it verbatim."""
        return f"{self.base_url}/v1"

    def anthropic_base_url(self) -> str:
        return f"{self.base_url}"

    def start(self) -> MockProvider:
        handler = _make_handler(self)
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-provider", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def __enter__(self) -> MockProvider:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── request handling ────────────────────────────────────────────────────
    def handle(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, list[tuple[bytes, float]]]:
        """Return (status, [(chunk_bytes, delay_before_send_s)]) for one request. Pure enough to unit-test."""
        self.scenario.requests += 1
        try:
            request = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {"_raw": body.decode("utf-8", "replace")}
        self.received.append({"path": path, "headers": dict(headers), "body": request})
        if self.scenario.status != 200:
            return self.scenario.status, [(self._error_body(429), 0.0)]
        anthropic = path.endswith("/messages")
        chunks = [self._chunk(i, anthropic) for i in range(self.scenario.chunks)]
        timed: list[tuple[bytes, float]] = []
        for index, chunk in enumerate(chunks):
            delay = self.scenario.first_byte_delay_ms / 1000 if index == 0 else self.scenario.delay_ms / 1000
            timed.append((chunk, delay))
            if self.scenario.error_after is not None and index + 1 == self.scenario.error_after:
                timed.append((self._error_event(anthropic), 0.0))
                break
        timed.append((self._done(anthropic), 0.0))
        return 200, timed

    # ── payloads ────────────────────────────────────────────────────────────
    def _chunk(self, index: int, anthropic: bool) -> bytes:
        text = self.scenario.chunk_text + ("" if self.scenario.body_bytes == 0 else "x" * self.scenario.body_bytes)
        if anthropic:
            event = {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": text}}
            return f"event: content_block_delta\ndata: {json.dumps(event)}\n\n".encode()
        event = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": self.scenario.model,
                 "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        return f"data: {json.dumps(event)}\n\n".encode()

    def _done(self, anthropic: bool) -> bytes:
        if anthropic:
            return b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
        return b"data: [DONE]\n\n"

    def _error_event(self, anthropic: bool) -> bytes:
        if anthropic:
            return b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error"}}\n\n'
        return b'data: {"error": {"message": "mock overloaded", "type": "server_error"}}\n\n'

    def _error_body(self, status: int) -> bytes:
        body = {"error": {"message": f"mock status {status}", "type": "rate_limit_error"}}
        return json.dumps(body).encode()


def _make_handler(provider: MockProvider) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:      # no stderr noise in tests
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            status, chunks = provider.handle(self.path, dict(self.headers), body)
            if status != 200:
                payload = chunks[0][0]
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            for sent, (chunk, delay) in enumerate(chunks):
                if provider.scenario.truncate_after is not None and sent >= provider.scenario.truncate_after:
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if delay:
                    time.sleep(delay)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        def do_GET(self) -> None:
            payload = json.dumps({"ok": True, "requests": provider.scenario.requests}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler
