"""P3B-01: the proxy forwards bytes it did not write, and records what came back without changing it.

The shape of these tests is deliberate. An origin server and the proxy live in one background event loop; the test body
speaks HTTP to the proxy with a **synchronous** client, so nothing here can pass because two coroutines agreed with each
other. What is asserted is what a client would see: the status, the headers, the body's sha256, and — for the recording
half — the frames a recovery scan finds on a real tape.

The one thing that cannot be observed from outside is the promise that recording is a *tap*: so the last test makes the
sink raise on every hook and asserts the response is byte-identical anyway, and the one before it puts a real
`TapeSink` behind a real `TapeWriter` and greps the tape.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from blackbox import constants as C
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ERROR_HEADER, ProxyServer
from blackbox.tape import scanner
from blackbox.tape import writer as writer_module

SSE_BODY = b"".join(
    b'data: {"choices":[{"delta":{"content":"tok%d"}}]}\n\n' % index for index in range(12)
)
JSON_BODY = json.dumps({"id": "chatcmpl-1", "choices": [{"message": {"content": "hi"}}]}).encode()


class Origin:
    """A stdlib origin that records what arrived and answers one of two shapes."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> Origin:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = (self._server.sockets or ())[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readuntil(b"\r\n")).rstrip(b"\r\n").decode()
            method, target, _ = line.split(" ")
            headers: list[tuple[str, str]] = []
            while True:
                raw = await reader.readuntil(b"\r\n")
                if raw in (b"\r\n", b"\n"):
                    break
                name, _, value = raw.rstrip(b"\r\n").decode().partition(":")
                headers.append((name.strip(), value.strip()))
            length = next((int(v) for k, v in headers if k.lower() == "content-length"), 0)
            body = await reader.readexactly(length) if length else b""
            self.requests.append({"method": method, "target": target,
                                  "headers": dict((k.lower(), v) for k, v in headers), "body": body})
            if "/sse" in target:
                await self._sse(writer)
            elif "/slow" in target:
                await asyncio.sleep(0.15)
                await self._plain(writer, 200, JSON_BODY, extra=[("x-blackbox-error", "leaked")])
            else:
                await self._plain(writer, 200, JSON_BODY, extra=[("x-blackbox-error", "leaked")])
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()

    async def _plain(self, writer: asyncio.StreamWriter, status: int, body: bytes,
                     extra: list[tuple[str, str]] = ()) -> None:            # type: ignore[assignment]
        head = [f"HTTP/1.1 {status} OK", f"Content-Length: {len(body)}", "Content-Type: application/json",
                "Connection: keep-alive", *[f"{k}: {v}" for k, v in extra]]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        await writer.drain()

    async def _sse(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        for index in range(0, len(SSE_BODY), 64):
            piece = SSE_BODY[index:index + 64]
            writer.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            await writer.drain()
            await asyncio.sleep(0.002)
        writer.write(b"0\r\n\r\n")
        await writer.drain()


class Box:
    """One event loop in one thread, hosting an origin and a proxy; the test body talks to it synchronously."""

    def __init__(self, routes_from: Any = None) -> None:
        self.origin = Origin()
        self.proxy: ProxyServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self._routes_from = routes_from
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Box:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(10), f"the box did not start: {self.error!r}"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)          # otherwise the thread runs forever and join() waits
        if self._thread is not None:
            self._thread.join(10)

    def _run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._start())
            self.ready.set()
            self.loop.run_forever()
        except BaseException as exc:                        # surfaced by the assertion in `__enter__`
            self.error = exc
            self.ready.set()

    async def _start(self) -> None:
        await self.origin.start()
        base = f"http://127.0.0.1:{self.origin.port}"
        table = self._routes_from(base) if self._routes_from else Routes(
            [Route(name="origin", base=f"{base}/base", prefix="/p"), Route(name="dead", base="http://127.0.0.1:9")],
            default=None)
        receiver = getattr(self, "receiver", None)
        self.proxy = await ProxyServer(table, receiver=receiver).start()

    async def _stop(self) -> None:
        assert self.proxy is not None
        await self.proxy.close()
        await self.origin.close()


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def _post(port: int, path: str, body: bytes, headers: dict[str, str] | None = None
          ) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers={"content-type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def test_a_plain_request_is_relayed_with_its_body_and_the_upstream_sees_the_routed_path():
    with Box() as box:
        assert box.proxy is not None
        status, headers, body = _post(box.proxy.port, "/p/json", b'{"model":"m"}')
        assert status == 200 and body == JSON_BODY
        assert headers["content-length"] == str(len(JSON_BODY))
        seen = box.origin.requests[-1]
        assert seen["target"] == "/base/json", "prefix stripped, base path kept"
        assert seen["body"] == b'{"model":"m"}'
        assert seen["headers"]["host"] == f"127.0.0.1:{box.origin.port}", "the upstream's own host, not the proxy's"


def test_an_sse_stream_is_byte_identical_and_still_chunked():
    with Box() as box:
        assert box.proxy is not None
        status, headers, body = _get(box.proxy.port, "/p/sse")
        assert status == 200
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(SSE_BODY).hexdigest(), \
            "what the client received is what the upstream pushed"
        assert body == SSE_BODY
        assert headers["content-type"] == "text/event-stream"
        assert "chunked" in headers["transfer-encoding"], "the stream is relayed as a stream, not buffered into a lump"
        assert box.proxy.stats.bytes_to_client == len(SSE_BODY)


def test_hop_by_hop_headers_and_ours_are_not_forwarded_in_either_direction():
    with Box() as box:
        assert box.proxy is not None
        status, headers, _body = _get(box.proxy.port, "/p/json", headers={
            "connection": "keep-alive", "proxy-connection": "keep-alive", "te": "trailers",
            "x-blackbox-run": "r_1", "x-blackbox-upstream-hint": "no", "x-request-id": "keep-me",
        })
        assert status == 200
        seen = box.origin.requests[-1]["headers"]
        for gone in ("proxy-connection", "te", "x-blackbox-run", "x-blackbox-upstream-hint"):
            assert gone not in seen, f"{gone} reached the upstream"
        assert seen["connection"] == "close", "the proxy's own connection header, not the client's keep-alive"
        assert seen["x-request-id"] == "keep-me"
        assert "x-blackbox-error" not in headers, "an x-blackbox-* header from upstream is not the client's business"


def test_an_unknown_upstream_is_a_502_that_says_why():
    with Box() as box:
        assert box.proxy is not None
        status, headers, body = _get(box.proxy.port, "/p/json", headers={"x-blackbox-upstream": "gemini"})
        assert status == 502
        assert headers[ERROR_HEADER] == "upstream-not-allowed"
        assert b"gemini" not in body, "the reason names the class, not the caller's input echoed back"
        assert box.proxy.stats.refused == 1


def test_a_dead_upstream_is_a_504_and_the_proxy_stays_up():
    with Box() as box:
        assert box.proxy is not None
        status, headers, _body = _get(box.proxy.port, "/p/json", headers={"x-blackbox-upstream": "dead"})
        assert status == 504 and headers[ERROR_HEADER] == "upstream-unreachable"
        status, _headers, body = _get(box.proxy.port, "/p/json")
        assert status == 200 and body == JSON_BODY, "one dead route does not take the listener with it"


def test_expect_100_continue_is_answered_before_the_body_is_read():
    with Box() as box:
        assert box.proxy is not None
        with socket.create_connection(("127.0.0.1", box.proxy.port), timeout=10) as sock:
            sock.sendall(b"POST /p/json HTTP/1.1\r\nHost: proxy\r\nContent-Length: 13\r\n"
                         b"Expect: 100-continue\r\n\r\n")
            interim = sock.recv(1024)
            assert interim.startswith(b"HTTP/1.1 100 Continue"), interim
            sock.sendall(b'{"model":"m"}')
            final = b""
            while b"\r\n\r\n" not in final:
                final += sock.recv(4096)
        assert final.startswith(b"HTTP/1.1 200"), final[:60]
        assert box.origin.requests[-1]["body"] == b'{"model":"m"}'


def test_keep_alive_serves_two_requests_on_one_connection():
    with Box() as box:
        assert box.proxy is not None
        with socket.create_connection(("127.0.0.1", box.proxy.port), timeout=10) as sock:
            for _ in range(2):
                sock.sendall(b"GET /p/json HTTP/1.1\r\nHost: proxy\r\nConnection: keep-alive\r\n\r\n")
                data = b""
                while b"}" not in data:
                    data += sock.recv(4096)
                assert data.startswith(b"HTTP/1.1 200"), data[:60]
        assert len(box.origin.requests) == 2


def test_the_tape_gets_request_chunk_and_response_frames_that_rebuild_the_body(tmp_path: Path):
    tape_dir = tmp_path / "tape"
    tape = writer_module.TapeWriter(str(tape_dir), segment_bytes=256 * 1024).open()
    uuid = tape.writer_uuid.hex()
    recorder = SinkRecorder(TapeSink(tape.emit, content="full", coalesce_bytes=1))
    box = Box()
    box.receiver = recorder
    try:
        with box:
            assert box.proxy is not None
            status, _headers, body = _get(box.proxy.port, "/p/sse")
            assert status == 200 and body == SSE_BODY
        tape.publish()
    finally:
        tape.close()

    result = scanner.scan(str(tape_dir), uuid)
    kinds = [C.KIND_NAMES[frame["kind"]] for frame in result.frames]
    assert kinds.count("MODEL_REQUEST") == 1
    assert kinds.count("MODEL_RESPONSE") == 1
    streamed = b"".join(frame["payload"] for frame in result.frames if frame["kind"] == C.KIND_MODEL_CHUNK)
    assert hashlib.sha256(streamed).hexdigest() == hashlib.sha256(SSE_BODY).hexdigest()
    summary = json.loads(next(f["payload"] for f in result.frames if f["kind"] == C.KIND_MODEL_RESPONSE))
    assert summary["status"] == 200 and summary["bytes_received"] == len(SSE_BODY)
    request = json.loads(next(f["payload"] for f in result.frames if f["kind"] == C.KIND_MODEL_REQUEST))
    assert request["provider"] == "origin" and request["path"] == "/base/sse"


class _Exploding:
    """A recorder that fails on every hook — the thing the proxy must survive."""

    def __init__(self) -> None:
        self.opened = 0
        self.received = 0

    def open_call(self, *args: Any, **kwargs: Any) -> Any:
        self.opened += 1
        raise RuntimeError("sink is on fire")

    def receive(self, *args: Any, **kwargs: Any) -> None:
        self.received += 1
        raise RuntimeError("sink is on fire")

    def close_call(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sink is on fire")


def test_a_recorder_that_raises_never_changes_the_response():
    exploding = _Exploding()
    box = Box()
    box.receiver = exploding
    try:
        with box:
            assert box.proxy is not None
            status, _headers, body = _get(box.proxy.port, "/p/sse")
            assert status == 200
            assert body == SSE_BODY, "recording is a tap; a broken tap must not change the water"
            assert box.proxy.stats.record_errors >= 1
    finally:
        pass
    assert exploding.opened == 1


def test_a_client_that_hangs_up_mid_stream_leaves_the_proxy_serving():
    with Box() as box:
        assert box.proxy is not None
        sock = socket.create_connection(("127.0.0.1", box.proxy.port), timeout=10)
        sock.sendall(b"GET /p/sse HTTP/1.1\r\nHost: proxy\r\n\r\n")
        assert sock.recv(256)
        sock.close()
        time.sleep(0.05)
        status, _headers, body = _get(box.proxy.port, "/p/json")
        assert status == 200 and body == JSON_BODY


@pytest.mark.parametrize("path", ["/p/json", "/p/json?stream=1"])
def test_the_query_string_survives_the_route(path: str):
    with Box() as box:
        assert box.proxy is not None
        _get(box.proxy.port, path)
        target = box.origin.requests[-1]["target"]
        assert target.startswith("/base/json")
        assert ("?" in target) == ("?" in path)
