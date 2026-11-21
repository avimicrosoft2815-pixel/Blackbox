"""What the proxy's event loop raised, kept where the next Windows sample can be read.

Defect 108 was a 686 ms rebind outlier whose job log carried one extra line: a `ProactorEventLoop` callback exception that
existed nowhere else. GitHub redirects job logs to a host this sandbox cannot reach, so the only durable place for that
fact is the proxy's own record. `ProxyThread.loop_errors` is that record, added on 2026-11-20 — and it landed without a
test, which is what this file is.

Two properties, and the second matters more than the first:

* a callback the loop could not attribute to a request is **kept** (message, exception type, handle type, when), so a
  later sample can say "ours" or "the runner's" instead of "an exception happened";
* the record is **bounded** — a loop that is failing every tick must not become the outage (256 entries and no growth
  beyond it), and the proxy must keep serving while it happens, because recording is the observer and never the fix.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from rebind_probe import LocalUpstream

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import ProxyThread


def _routes(base: str) -> Routes:
    return Routes([Route(name="openai", base=base, prefix="/openai")], default="openai")


@pytest.fixture()
def upstream() -> Iterator[LocalUpstream]:
    with LocalUpstream() as server:
        yield server


def _raise(_proxy: ProxyThread) -> None:
    raise RuntimeError("a callback the loop could not attribute to a request")


def _wait_for(count: int, proxy: ProxyThread, deadline_s: float = 10.0) -> bool:
    give_up = time.monotonic() + deadline_s
    while time.monotonic() < give_up:
        if len(proxy.loop_errors) >= count:
            return True
        time.sleep(0.005)
    return False


def _get(port: int, path: str = "/openai/v1/models") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: proxy\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    return b"".join(chunks)


def test_a_callback_that_raises_is_recorded_and_the_proxy_keeps_serving(upstream: LocalUpstream) -> None:
    proxy = ProxyThread(_routes(upstream.base), supervise_ms=0.0)
    proxy.start()
    try:
        loop = proxy._loop
        assert loop is not None, "the loop is running behind the thread"
        loop.call_soon_threadsafe(_raise, proxy)
        assert _wait_for(1, proxy), f"nothing was recorded: {proxy.loop_errors!r}"

        record = proxy.loop_errors[0]
        assert record["exception"] == "RuntimeError", record
        assert "callback" in record["message"].lower(), (
            f"the message has to say where it came from, otherwise it is the Windows sample again: {record}")
        assert isinstance(record["handle"], str) and record["handle"], record
        assert isinstance(record["when"], float) and record["when"] > 0, record

        # Recording is the observer, never the fix: a loop that logged an exception still answers requests.
        answer = _get(proxy.port)
        assert b"200" in answer.split(b"\r\n", 1)[0], answer[:200]
    finally:
        proxy.stop()


def test_the_record_is_bounded_so_a_failing_loop_does_not_become_the_outage(upstream: LocalUpstream) -> None:
    proxy = ProxyThread(_routes(upstream.base), supervise_ms=0.0)
    proxy.start()
    try:
        loop = proxy._loop
        assert loop is not None
        for _ in range(300):
            loop.call_soon_threadsafe(_raise, proxy)
        assert _wait_for(256, proxy), f"the cap was not reached: {len(proxy.loop_errors)}"
        time.sleep(0.25)                       # let the remaining forty callbacks run before the count is read
        assert len(proxy.loop_errors) == 256, (
            f"the record grew past its bound ({len(proxy.loop_errors)}), which is how a log becomes an outage")
        answer = _get(proxy.port)
        assert b"200" in answer.split(b"\r\n", 1)[0], answer[:200]
    finally:
        proxy.stop()


def test_an_unused_proxy_records_nothing(upstream: LocalUpstream) -> None:
    """The control: a record that fills itself is not a record of anything."""
    proxy = ProxyThread(_routes(upstream.base), supervise_ms=0.0)
    proxy.start()
    try:
        assert _get(proxy.port)
        time.sleep(0.05)
        assert proxy.loop_errors == [], proxy.loop_errors
    finally:
        proxy.stop()


def test_a_rebind_records_its_cpu_time_beside_its_wall_time(upstream: LocalUpstream, monkeypatch) -> None:
    """The pair that reads a defect-108 outlier from the tape: wall says how long, CPU says whether we were running.

    `rebind()` measures a cross-thread `.result()` wait, so wall time includes however long the loop thread waited to be
    scheduled. A 686 ms sample with 3 ms of process CPU is a machine that starved the process; the same 686 ms with
    600 ms of CPU is our own work. Before this, every sample looked identical and the only way to tell them apart was a
    tool reading a number that was never recorded.
    """
    proxy = ProxyThread(_routes(upstream.base), supervise_ms=0.0)
    proxy.start()
    try:
        # A rebind that *waits* rather than works: the loop is asked to restart the listener after a sleep on this thread,
        # so wall climbs and process CPU does not. `_restart` is the seam, and it is the same call the real path makes.
        real_restart = proxy._restart

        def slow_restart(loop, timeout):
            time.sleep(0.12)
            return real_restart(loop, timeout)

        monkeypatch.setattr(proxy, "_restart", slow_restart)
        wall_ms = proxy.rebind()
        assert wall_ms >= 100.0, f"the seam did not slow the rebind: {wall_ms:.1f} ms"
        assert len(proxy.rebind_cpu_ms) == len(proxy.rebinds) == 1
        cpu_ms = proxy.rebind_cpu_ms[0]
        assert cpu_ms < wall_ms / 2, (
            f"a sleeping rebind must not look like work: wall {wall_ms:.1f} ms, cpu {cpu_ms:.1f} ms")

        # And the other direction: a rebind that burns CPU shows it. The loop's own work is small, so this measures the
        # waiting thread spinning, which is what a busy sample would also contain.
        monkeypatch.setattr(proxy, "_restart", real_restart)

        def busy_restart(loop, timeout):
            deadline = time.process_time() + 0.12
            while time.process_time() < deadline:
                pass
            return real_restart(loop, timeout)

        monkeypatch.setattr(proxy, "_restart", busy_restart)
        wall_ms = proxy.rebind()
        assert proxy.rebind_cpu_ms[1] > 80.0, (
            f"a rebind that spent CPU must record it: cpu {proxy.rebind_cpu_ms[1]:.1f} ms of {wall_ms:.1f} ms")
        assert len(proxy.rebinds) == len(proxy.rebind_cpu_ms) == 2, "one CPU sample per wall sample, always"
    finally:
        proxy.stop()
