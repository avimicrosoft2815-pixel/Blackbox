"""B3 in `docs/RELEASE-BLOCKERS.md`: what the proxy does *at* the connection limit.

Before this, the ceiling was whatever the descriptor table happened to allow, and what happened past it was nothing the
child could read: `accept` failed with `EMFILE`, asyncio logged a traceback per attempt, and a probe issued during a
450-request burst **timed out after 4 s**. Raising the soft limit (`proxy/limits.py`) moved the cliff; these tests are
about removing the *silence*, which is the part a user cannot act on.

The decision, recorded in the module docstring and in `docs/defects.md` (defect 62):

* a request past `MAX_INFLIGHT` **waits** for a slot — backpressure, because refusing a request the upstream would have
  answered breaks the pass-through promise this proxy exists for. The wait is bounded (`QUEUE_WAIT_S`) and measured
  (`stats.queued`, `stats.max_queue_wait_ms`).
* a *connection* past `MAX_CONNECTIONS` is answered with `503` and `x-blackbox-error: connection-limit: …` in
  milliseconds — the one case where the recorder genuinely cannot keep its promise, so it says so.
* both numbers are small enough that the worst case fits the smallest default descriptor budget this project meets
  (1024), even where `limits.py` could not raise anything. That is arithmetic, and it is asserted here.

The proxy is driven with the ceilings turned down (2, 1, 8) so a test *is* a burst at a real ceiling; the behaviour does
not depend on the size of the number. Clients are synchronous, in their own threads, so what is asserted is what a child
process would see.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import (
    ERROR_HEADER,
    FD_BUDGET,
    MAX_CONNECTIONS,
    MAX_INFLIGHT,
    MIN_CONNECTIONS,
    MIN_INFLIGHT,
    PROXY_LOCAL_SLACK_FDS,
    WORST_CASE_FDS,
    ProxyServer,
    plan_caps,
)

BODY = b'{"id":"chatcmpl-test","choices":[{"message":{"content":"hi"}}]}'


class GatedOrigin:
    """An origin that parks every request until it is released, and counts how many are parked at once."""

    def __init__(self) -> None:
        self.port = 0
        self.active = 0
        self.peak = 0
        self.served = 0
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate = asyncio.Event()
        self._wanted: tuple[int, threading.Event] | None = None

    async def start(self) -> GatedOrigin:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int((self._server.sockets or ())[0].getsockname()[1])
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def release(self) -> None:
        # Called from the test thread: `Event.set()` is a loop operation, and setting it from here without scheduling is
        # a wakeup that never happens — which is exactly the stall the first draft of this file measured.
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._gate.set)

    def await_parked(self, count: int, timeout: float = 10.0) -> bool:
        """Block until `count` requests are parked in the origin, or the timeout expires."""
        event = threading.Event()
        self._wanted = (count, event)
        if self.active >= count:                              # already there: the handler's edge has been passed
            return True
        return event.wait(timeout)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    name, _, value = line.decode("latin-1").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip() or 0)
                body = await reader.readexactly(length) if length else b""
                assert isinstance(body, bytes)
                self.active += 1
                self.peak = max(self.peak, self.active)
                wanted = self._wanted
                if wanted is not None and self.active >= wanted[0]:
                    wanted[1].set()
                try:
                    await self._gate.wait()
                finally:
                    self.active -= 1
                self.served += 1
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(BODY)).encode() + b"\r\nConnection: keep-alive\r\n\r\n"
                             + BODY)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


class Bench:
    """One event loop in one thread: a gated origin, and a proxy whose ceilings are the test's."""

    def __init__(self, *, max_inflight: int = MAX_INFLIGHT, max_connections: int = MAX_CONNECTIONS,
                 queue_wait_s: float = 60.0) -> None:
        self.origin = GatedOrigin()
        self.proxy: ProxyServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.max_inflight = max_inflight
        self.max_connections = max_connections
        self.queue_wait_s = queue_wait_s
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Bench:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(15), f"the bench did not start: {self.error!r}"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        assert self._thread is not None
        self._thread.join(15)

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
        table = Routes([Route(name="openai", base=f"http://127.0.0.1:{self.origin.port}")], default="openai")
        self.proxy = await ProxyServer(table, max_inflight=self.max_inflight,
                                       max_connections=self.max_connections,
                                       queue_wait_s=self.queue_wait_s).start()

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

    @property
    def port(self) -> int:
        assert self.proxy is not None
        return self.proxy.port

    def stats(self) -> dict[str, Any]:
        assert self.proxy is not None and self.loop is not None

        async def read() -> dict[str, Any]:
            assert self.proxy is not None
            return self.proxy.stats.as_dict()

        return asyncio.run_coroutine_threadsafe(read(), self.loop).result(10)


def _request(port: int, *, timeout: float = 15.0, path: str = "/v1/chat/completions"
             ) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("POST", path, body=b"{}", headers={"content-type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


class Caller(threading.Thread):
    """One child's request, in its own thread: the test never awaits the thing it is measuring."""

    def __init__(self, port: int, *, timeout: float = 15.0) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.timeout = timeout
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body: bytes = b""
        self.error: BaseException | None = None
        self.finished_at: float | None = None

    def run(self) -> None:
        try:
            self.status, self.headers, self.body = _request(self.port, timeout=self.timeout)
        except BaseException as exc:                        # a hang shows up as a timeout, which is the point
            self.error = exc
        finally:
            self.finished_at = time.monotonic()


def test_a_request_past_the_cap_waits_and_is_served_not_refused() -> None:
    """Backpressure: six requests against `max_inflight=2` — all six are answered, two at a time, none refused."""
    with Bench(max_inflight=2, max_connections=64) as bench:
        callers = [Caller(bench.port) for _ in range(6)]
        for caller in callers:
            caller.start()
        assert bench.origin.await_parked(2, timeout=10), "two requests should reach the origin; the rest wait"
        time.sleep(0.25)                                     # give the four queued requests time to arrive and park
        assert bench.origin.peak <= 2, f"the cap leaked: {bench.origin.peak} requests in flight"
        bench.origin.release()
        for caller in callers:
            caller.join(20)
        statuses = [caller.status for caller in callers]
        assert statuses == [200] * 6, statuses
        assert bench.origin.served == 6
        stats = bench.stats()
        assert stats["refused"] == 0, stats
        assert stats["queued"] >= 1, "a wait that happened must be counted, or the run cannot report it"
        assert stats["max_queue_wait_ms"] > 0, stats


def test_a_connection_past_the_limit_is_answered_in_milliseconds() -> None:
    """The one case the recorder cannot serve: a 503 with a reason a user can act on, not a connection that hangs."""
    with Bench(max_inflight=8, max_connections=2) as bench:
        holders = [Caller(bench.port) for _ in range(2)]
        for holder in holders:
            holder.start()
        assert bench.origin.await_parked(2, timeout=10)
        started = time.monotonic()
        status, headers, body = _request(bench.port, timeout=10)
        elapsed = time.monotonic() - started
        assert status == 503, (status, headers)
        assert headers.get(ERROR_HEADER, "").startswith("connection-limit"), headers
        assert "holds its 2 connections" in headers[ERROR_HEADER], headers
        # a rejection has to be readable by the developer whose request it was, not only by a status code
        assert b"retry when its in-flight requests finish, or run fewer at once" in body, body
        assert elapsed < 2.0, f"a refused connection took {elapsed:.2f}s; the point is that it does not wait"
        stats = bench.stats()
        assert stats["refused_reasons"].get("connection-limit") == 1, stats
        bench.origin.release()
        for holder in holders:
            holder.join(20)


def test_a_wait_that_would_outlive_the_bound_is_answered_not_stalled() -> None:
    """An unbounded queue is the silent stall this blocker is about; past `queue_wait_s` the answer is a 503."""
    with Bench(max_inflight=1, max_connections=8, queue_wait_s=0.3) as bench:
        first = Caller(bench.port)
        first.start()
        assert bench.origin.await_parked(1, timeout=10)
        started = time.monotonic()
        status, headers, body = _request(bench.port, timeout=10)
        elapsed = time.monotonic() - started
        assert status == 503, (status, headers)
        assert headers.get(ERROR_HEADER, "").startswith("queue-timeout"), headers
        assert b"slower than the recorder's queue allowance: retry, or run fewer at once" in body, body
        assert 0.2 <= elapsed < 5.0, f"answered in {elapsed:.2f}s for a 0.3s bound"
        assert bench.stats()["refused_reasons"].get("queue-timeout") == 1
        bench.origin.release()
        first.join(20)
        assert first.status == 200


def test_a_silent_connection_at_the_limit_is_closed_not_held() -> None:
    """A client that connects and says nothing past the limit costs two seconds and a descriptor, never a slot."""
    import socket

    with Bench(max_inflight=8, max_connections=1) as bench:
        holder = Caller(bench.port)
        holder.start()
        assert bench.origin.await_parked(1, timeout=10)
        quiet = socket.create_connection(("127.0.0.1", bench.port), timeout=10)
        try:
            started = time.monotonic()
            assert quiet.recv(4096).startswith(b"HTTP/1.1 503"), "a silent connection must be told, not left to hang"
            assert time.monotonic() - started < 5.0
        finally:
            quiet.close()
        bench.origin.release()
        holder.join(20)


def test_the_caps_fit_the_smallest_descriptor_budget_we_meet() -> None:
    """The numbers, as arithmetic: the worst case must fit 1024 descriptors, where no raise was possible at all."""
    assert MAX_INFLIGHT >= 200, "§15.1 asks for 200 concurrent streams; the cap must not be below the requirement"
    assert MAX_CONNECTIONS >= 256
    assert WORST_CASE_FDS + PROXY_LOCAL_SLACK_FDS <= FD_BUDGET, (
        f"{WORST_CASE_FDS} + {PROXY_LOCAL_SLACK_FDS} exceeds the {FD_BUDGET}-descriptor default soft limit")


def test_a_host_that_could_not_raise_its_limit_gets_caps_that_fit_it() -> None:
    """The documented pair is a *plan for 1024 descriptors*, not a constant: a macOS shell starting at 256, or a container
    whose hard limit nobody can pass, must not be promised 808 descriptors it does not have.

    Every row is arithmetic the proxy can check about itself before it accepts a connection.
    """
    plan = plan_caps(FD_BUDGET)
    assert (plan.max_inflight, plan.max_connections) == (MAX_INFLIGHT, MAX_CONNECTIONS)
    assert "fits the 1024 descriptors this host allows" in plan.reason

    generous = plan_caps(65536)
    assert (generous.max_inflight, generous.max_connections) == (MAX_INFLIGHT, MAX_CONNECTIONS)
    assert generous.budget == 65536

    macos_shell = plan_caps(256)                    # the number in limits.py's docstring, and the case that made this a plan
    assert macos_shell.max_connections < MAX_CONNECTIONS and macos_shell.max_inflight < MAX_INFLIGHT
    assert macos_shell.max_connections + macos_shell.max_inflight + PROXY_LOCAL_SLACK_FDS <= 256, macos_shell
    assert "256 descriptors" in macos_shell.reason

    small = plan_caps(128)
    assert small.max_connections + small.max_inflight + PROXY_LOCAL_SLACK_FDS <= 128, small
    assert small.max_connections >= MIN_CONNECTIONS and small.max_inflight >= MIN_INFLIGHT

    # Below the floors there is no pair that fits; the caps stop tracking the budget and the reason says so, because the
    # 503 at them is the only thing left protecting the process.
    tiny = plan_caps(64)
    assert (tiny.max_connections, tiny.max_inflight) == (MIN_CONNECTIONS, MIN_INFLIGHT), tiny
    assert "below the" in tiny.reason and "protects the process" in tiny.reason

    # Windows: no readable budget. The documented pair stands as policy, and the sentence does not claim arithmetic.
    windows = plan_caps(None)
    assert (windows.max_inflight, windows.max_connections, windows.budget) == (MAX_INFLIGHT, MAX_CONNECTIONS, None)
    assert "no descriptor budget" in windows.reason


def test_a_server_built_on_a_small_host_uses_the_plan_and_says_so(monkeypatch: Any) -> None:
    """The planning is wired into construction, not just exported: `stats.caps` is what the run's sentence reads."""
    import blackbox.proxy.server as server_module

    monkeypatch.setattr(server_module, "fd_budget", lambda: 256)
    table = Routes([Route(name="openai", base="http://127.0.0.1:9")], default="openai")
    server = ProxyServer(table)
    assert server.max_connections + server.max_inflight + PROXY_LOCAL_SLACK_FDS <= 256
    caps = server.stats.as_dict()["caps"]
    assert caps["fd_budget"] == 256 and caps["max_connections"] == server.max_connections
    assert "256 descriptors" in caps["why"]
