"""The outage clock, tested against the ways a clock about downtime can lie.

Defect 60's failure was a 103.2 ms reading that was mostly the *harness's* thread hop, so the first revision started its
clock when `close()` returned. That is defensible but optimistic: the listening socket stops accepting **inside**
`close()`, and a request can be refused while the coroutine is still running (`wait_closed()`, the pool drain, a loop
thread descheduled before the caller wakes). The honest clock therefore does not live in our threads at all — it is a
real connection and a real request, on the port the child was told.

It also separates the two questions a single number kept conflating: whether the **listener** is there (connect
refused / accepted — the gap, and what the 100 ms budget is about) and whether a **connection is answered** (a request
in flight when the listener goes away can hang until its own timeout — a real casualty, reported as `service_gap_ms`,
never added to the gap). A connect-only probe was tried for the gap and rejected: a closed listening socket keeps
completing handshakes from its accept queue, so connections are accepted for tens of milliseconds while nothing answers
them.

The test that matters is `test_the_clock_counts_downtime_inside_close_before_it_returns`: it makes `close()` spend
60 ms *after* the socket is gone, then asserts the measured gap contains those 60 ms while the post-close clock
reports almost nothing. A clock that cannot see this interval cannot be trusted with a 100 ms budget.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from rebind_probe import ServingProbe, measure_outage, try_serve

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer
from blackbox.proxy.supervisor import ProxyThread


def _table() -> Routes:
    return Routes([Route(name="openai", base="http://127.0.0.1:9", prefix="/openai")], default="openai")


def _probe(port: int) -> ServingProbe:
    """Three requests in flight (the default): one hung request cannot blind the measurement."""
    return ServingProbe(port)


def test_an_answered_request_is_served_and_a_refused_one_is_not() -> None:
    with ProxyThread(_table()) as proxy:
        served = try_serve(proxy.port)
        assert served.served is True, served
        assert served.detail.startswith("HTTP 5"), (
            f"the upstream is a closed port; any answer from the proxy is service, and it said {served.detail}")
        port = proxy.port
    refused = try_serve(port, timeout=0.2)
    assert refused.served is False, "after the listener is gone the port must refuse"
    assert refused.connected is False, "a refused connect is not even a connection"


def test_the_clock_counts_downtime_inside_close_before_it_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interval the post-close clock cannot see: the socket is gone, the coroutine has not returned yet."""
    original = ProxyServer.close
    stall_s = 0.060
    stalled: dict[str, bool] = {"done": False}

    async def slow_close(self: ProxyServer) -> None:
        await original(self)
        if not stalled["done"]:      # only the break stalls: the restore's own close must not hide its start
            stalled["done"] = True
            time.sleep(stall_s)      # the listener is already closed here; the caller is still waiting

    with ProxyThread(_table()) as proxy, _probe(proxy.port) as probe:
        server, loop = proxy.server, proxy._loop
        assert server is not None and loop is not None
        time.sleep(0.02)                                  # let the probes see a serving listener first
        monkeypatch.setattr(ProxyServer, "close", slow_close)
        close_returned: list[float] = []
        sampled = measure_outage(probe, lambda: (
            asyncio.run_coroutine_threadsafe(server.close(), loop).result(5),
            close_returned.append(time.perf_counter()))[-1], timeout=5.0)
        monkeypatch.setattr(ProxyServer, "close", original)
    assert sampled["observed_failure"] is True, "the listener was gone for 60 ms; a connect must have been refused"
    assert isinstance(sampled["outage_ms"], float)
    assert sampled["outage_ms"] >= stall_s * 1000.0 * 0.8, (
        f"{stall_s * 1000:.0f} ms of downtime happened before close() returned; the clock reported "
        f"{sampled['outage_ms']:.1f} ms")
    # What the optimistic clock would have said: from `close()` returning to the first *answered* request.
    served_after = sampled["served_again"]
    assert isinstance(served_after, float), "a request must have been answered again after the restore"
    post_close_ms = (served_after - close_returned[0]) * 1000.0
    missed = sampled["outage_ms"] - post_close_ms
    assert missed >= stall_s * 1000.0 / 2, (
        f"the post-close clock would have reported {post_close_ms:.1f} ms of the {sampled['outage_ms']:.1f} ms outage, "
        f"missing {missed:.1f} ms of it — which is why the outage is measured by connections, not by when a coroutine "
        "returned")


def test_no_outage_is_reported_when_nothing_is_refused() -> None:
    """A measurement that invents downtime is as useless as one that hides it."""
    with ProxyThread(_table()) as proxy, _probe(proxy.port) as probe:
        time.sleep(0.02)
        sampled = measure_outage(probe, lambda: time.sleep(0.01), timeout=2.0)
        cycle = float(sampled["probe_cycle_ms"])
    assert isinstance(sampled["outage_upper_ms"], float)
    # The break here takes nothing away, so whatever the probe saw, it cannot have been a long outage. (A stray reset
    # of one loopback connect is possible and would make `observed_failure` true; that is the probe being honest, not
    # the measurement inventing downtime.)
    assert sampled["outage_upper_ms"] <= max(12.0, 9 * cycle), sampled
    if not sampled["observed_failure"]:
        assert sampled["outage_ms"] == sampled["outage_upper_ms"], "with no refusal both clocks are the same span"


def test_the_upper_bound_is_the_conservative_one() -> None:
    with ProxyThread(_table()) as proxy, _probe(proxy.port) as probe:
        server, loop = proxy.server, proxy._loop
        assert server is not None and loop is not None
        time.sleep(0.02)
        sampled = measure_outage(probe, lambda: asyncio.run_coroutine_threadsafe(server.close(), loop).result(5))
    assert isinstance(sampled["outage_ms"], float) and isinstance(sampled["outage_upper_ms"], float)
    assert sampled["outage_upper_ms"] >= sampled["outage_ms"] >= 0.0
    if sampled["observed_failure"]:                     # a fast loopback rebind can finish inside one probe cycle
        assert int(sampled["refused_requests"]) >= 1
    assert sampled["outage_upper_ms"] < 100.0, f"a loopback rebind took {sampled['outage_upper_ms']:.1f} ms"


def test_a_listener_that_never_comes_back_is_an_error_not_a_zero() -> None:
    with ProxyThread(_table()) as proxy, _probe(proxy.port) as probe:
        server, loop = proxy.server, proxy._loop
        assert server is not None and loop is not None
        time.sleep(0.02)
        proxy._stopping.set()               # the loop will refuse to repair; nothing comes back
        with pytest.raises(RuntimeError, match="did not come back"):
            measure_outage(probe, lambda: asyncio.run_coroutine_threadsafe(server.close(), loop).result(5),
                           timeout=0.4)


def test_a_probe_on_a_port_nobody_serves_cannot_measure_an_outage() -> None:
    probe = _probe(9).start()
    try:
        with pytest.raises(RuntimeError, match="not measuring a serving listener"):
            measure_outage(probe, lambda: None, timeout=0.3)
    finally:
        probe.stop()


def test_the_probe_reports_its_own_resolution() -> None:
    """Every number out of this module is bounded by the probe's cycle, and the cycle is printed beside it."""
    with ProxyThread(_table()) as proxy, ServingProbe(proxy.port) as probe:
        time.sleep(0.05)
        cycle = probe.cycle_ms()
        attempts = probe.since(0)
    assert attempts and all(attempt.served for attempt in attempts)
    assert 0.0 < cycle < 50.0, f"one loopback request took {cycle:.2f} ms; that is the resolution of every number here"
