"""An origin that streams slowly and one that dies mid-body, plus the one-event-loop box the tests talk to synchronously."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer

JSON_BODY = json.dumps({"id": "chatcmpl-d", "choices": [{"message": {"content": "hi"}}]}).encode()

# Big enough that the proxy cannot swallow it into a socket buffer in one write, so a client that has gone away is
# noticed while the upstream is still producing — which is the evidence INTERRUPTED_CLIENT is made of.
STREAM_CHUNKS = 512
STREAM_PIECE = b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n' * 8      # ~400 B per chunk, ~200 KiB total
TRUNCATE_DECLARED = 8192
TRUNCATE_SENT = 512


class Origin:
    """Serves `/stream` slowly, `/truncate` short, and `/ok` normally; records what arrived."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stream_body = STREAM_PIECE * STREAM_CHUNKS          # what a client should end up with, byte for byte
        self.port = 0
        self.sent_stream_bytes = 0
        self.stream_finished = threading.Event()
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
            while True:
                try:
                    line = (await reader.readuntil(b"\r\n")).rstrip(b"\r\n").decode()
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                if not line:
                    return
                method, target, _version = line.split(" ")
                headers: list[tuple[str, str]] = []
                while True:
                    raw = await reader.readuntil(b"\r\n")
                    if raw in (b"\r\n", b"\n"):
                        break
                    name, _, value = raw.rstrip(b"\r\n").decode().partition(":")
                    headers.append((name.strip(), value.strip()))
                length = next((int(v) for k, v in headers if k.lower() == "content-length"), 0)
                body = await reader.readexactly(length) if length else b""
                self.requests.append({"method": method, "target": target, "body": body})
                if target.endswith("/stream"):
                    await self._stream(writer)
                elif target.endswith("/truncate"):
                    await self._truncate(writer)
                else:
                    await self._plain(writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()

    async def _plain(self, writer: asyncio.StreamWriter) -> None:
        head = ["HTTP/1.1 200 OK", f"Content-Length: {len(JSON_BODY)}", "Content-Type: application/json",
                "Connection: keep-alive"]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + JSON_BODY)
        await writer.drain()

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        try:
            for index in range(STREAM_CHUNKS):
                writer.write(f"{len(STREAM_PIECE):x}\r\n".encode() + STREAM_PIECE + b"\r\n")
                self.sent_stream_bytes += len(STREAM_PIECE)
                await writer.drain()
                if index % 8 == 0:
                    await asyncio.sleep(0.002)
        except (ConnectionError, OSError):
            return                             # the proxy let go of us: the client must have gone, which is the subject
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        self.stream_finished.set()

    async def _truncate(self, writer: asyncio.StreamWriter) -> None:
        """Declare a body and send a fraction of it, then close: the provider-side failure of §14.4a."""
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {TRUNCATE_DECLARED}\r\n"
                     f"Content-Type: application/json\r\nConnection: keep-alive\r\n\r\n".encode()
                     + b"x" * TRUNCATE_SENT)
        await writer.drain()
        writer.close()


class Box:
    """Origin and proxy on one background loop; the test body talks to it synchronously."""

    def __init__(self, receiver: Any = None) -> None:
        self.receiver = receiver
        self.origin = Origin()
        self.proxy: ProxyServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Box:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(10), f"the box did not start: {self.error!r}"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
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
        table = Routes([Route(name="origin", base=f"http://127.0.0.1:{self.origin.port}", prefix="/p")], default=None)
        self.proxy = await ProxyServer(table, receiver=self.receiver).start()

    async def _stop(self) -> None:
        assert self.proxy is not None
        await self.proxy.close()
        await self.origin.close()
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
