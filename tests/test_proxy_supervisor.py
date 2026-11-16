"""P3B-03, the half that does not need a child process: the variable plan, and a listener that comes back.

Two promises are tested here, and they are the two that make the proxy safe to hand a launcher:

* **Nothing the child already chose is overwritten.** A base-URL variable that is set stays set, and its destination
  becomes a route — because the alternative is either a recorder that silently redirects traffic or a proxy that
  refuses to reach the destination the user named.
* **A dead listener comes back inside the budget**, on the port the child was told, and the watchdog is what notices.
  The budget is gated on watchdog-triggered samples only, every one of them, because a median over manual rebinds
  measured a different operation and hid the outliers that matter.
"""
from __future__ import annotations

import asyncio
import time

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import BASE_URL_VARS, ENV_PROXY, REBIND_BUDGET_MS, ProxyThread, plan_exports


def _table() -> Routes:
    return Routes([Route(name="openai", base="http://127.0.0.1:9", prefix="/openai"),
                   Route(name="anthropic", base="http://127.0.0.1:9", prefix="/anthropic")],
                  default="openai")


def test_an_unset_variable_is_exported_and_names_the_proxy():
    plan = plan_exports({}, "http://127.0.0.1:1234")
    assert plan.exports["OPENAI_BASE_URL"] == "http://127.0.0.1:1234/openai/v1", \
        "the OpenAI SDK appends only /chat/completions, so its base_url must carry /v1 (defect 39)"
    assert plan.exports["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234/anthropic"
    assert plan.exports[ENV_PROXY] == "1"
    assert plan.adopted == {}
    assert plan.routes == [], "nothing to adopt means nothing extra the proxy may reach"
    assert plan.default_route == "openai"


def test_a_set_variable_is_left_alone_and_becomes_an_upstream():
    env = {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1", "ANTHROPIC_BASE_URL": "   "}
    plan = plan_exports(env, "http://127.0.0.1:1234")
    assert "OPENAI_BASE_URL" not in plan.exports, "the user's own value is never overwritten"
    assert plan.adopted["OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert [route.name for route in plan.routes] == ["openai-own"]
    assert plan.routes[0].destination == ("127.0.0.1", 11434)
    assert plan.exports["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234/anthropic", "blank is unset"


def test_a_value_that_is_not_a_url_is_adopted_but_not_routed():
    plan = plan_exports({"OLLAMA_HOST": "127.0.0.1:11434"}, "http://127.0.0.1:1234")
    assert plan.adopted["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert plan.routes == [], "a bare host:port is not a base URL, and the proxy does not guess one"
    assert plan.exports["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:1234/gemini"
    assert plan.default_route == "openai", "the first variable that was ours to point"


def test_when_every_variable_was_already_set_the_default_is_the_first_adopted_route_not_a_fabrication():
    env = {var: f"http://127.0.0.1:{8000 + index}/v1" for index, (var, _name) in enumerate(BASE_URL_VARS)}
    plan = plan_exports(env, "http://127.0.0.1:1234")
    assert plan.exports == {ENV_PROXY: "1"}, "nothing was pointed at the proxy, so nothing may claim to be"
    assert plan.default_route == "openai-own"


def test_every_variable_the_plan_names_is_covered():
    plan = plan_exports({}, "http://127.0.0.1:1234")
    for var, _name in BASE_URL_VARS:
        assert var in plan.exports


def test_the_thread_listens_starts_and_stops_cleanly():
    with ProxyThread(_table()) as proxy:
        assert proxy.port > 0
        assert proxy.url.startswith("http://127.0.0.1:")
        assert proxy.stats is not None
        assert proxy.stats.requests == 0
    assert proxy.server is None or not proxy.server.is_serving


def test_a_rebind_keeps_the_port_the_child_was_told():
    with ProxyThread(_table()) as proxy:
        port = proxy.port
        elapsed = proxy.rebind()
        assert proxy.port == port, "the child was told this port; a rebind that changes it is a broken child"
        assert proxy.rebinds == [elapsed]
        # The elapsed time is recorded here and published in `bench/results/2026-11-16-watchdog.json`, but it is not
        # the gate: a manual rebind measures this runner's scheduler as much as it measures the proxy (defect 40 — a
        # macOS arm64 cell once read 107.9 ms). The gate is the next test, where the sample is watchdog-triggered and
        # every one of them counts.
        assert elapsed >= 0.0


WATCHDOG_SAMPLES = 10


def _wait_for_restore(proxy: ProxyThread, dead, deadline: float) -> bool:
    """True when service is back on a listener that is *not* the one we closed."""
    while time.monotonic() < deadline:
        current = proxy.server
        if current is not None and current is not dead and current.is_serving:
            return True
        time.sleep(0.002)
    return False


def test_every_watchdog_recovery_stays_inside_the_budget_and_every_sample_is_kept():
    """P3B-03's gate, measured the only way that means anything: real watchdog recoveries, every sample asserted.

    The previous version of this test closed the listener once and then performed four *manual* rebinds, asserting the
    median of five. That median was doing two kinds of harm: three of its five samples were of a different operation
    than the one being promised, and taking a median is precisely how a 107.9 ms outlier disappears. Here each sample
    comes from the watchdog itself, both halves of the latency are checked (`rebind_ms` for the restart, `watchdog_ms`
    for wake-to-service), and one bad sample fails the test.
    """
    with ProxyThread(_table(), supervise_ms=10.0) as proxy:
        port = proxy.port
        loop = proxy._loop
        assert loop is not None
        for index in range(WATCHDOG_SAMPLES):
            server = proxy.server
            assert server is not None and server.is_serving
            # Close the listener behind the supervisor's back: exactly the failure it exists for.
            asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
            assert not server.is_serving
            assert _wait_for_restore(proxy, server, time.monotonic() + 2.0), \
                f"the watchdog did not restore service on recovery {index}"
            assert proxy.port == port, "and it restored it on the same port"
        evidence = proxy.watchdog_stats()
        assert evidence["alive"], "the watchdog thread must survive the recoveries it performs"
        assert evidence["errors"] == [], f"a recovery attempt failed: {evidence['errors']}"
        assert len(evidence["recoveries"]) == WATCHDOG_SAMPLES, (
            "one recovery record per death, and none of them manual: "
            f"{len(evidence['recoveries'])} recorded for {WATCHDOG_SAMPLES} deaths")
        assert proxy.rebinds == [r["rebind_ms"] for r in evidence["recoveries"]], \
            "no rebind happened outside the watchdog, or a sample went missing"
        for record in evidence["recoveries"]:
            assert record["port"] == port and record["port_changed"] is False
            assert record["rebind_ms"] <= REBIND_BUDGET_MS, (
                f"watchdog rebind took {record['rebind_ms']:.1f} ms, budget {REBIND_BUDGET_MS} ms; "
                f"all samples {proxy.rebinds}")
            # Wake-to-service is what the child actually experiences; the tick is its lower bound.
            assert record["watchdog_ms"] <= REBIND_BUDGET_MS, (
                f"watchdog recovery took {record['watchdog_ms']:.1f} ms, budget {REBIND_BUDGET_MS} ms; "
                f"all records {evidence['recoveries']}")
        # Published as raw samples, not as a median: `python tools/bench_watchdog.py` regenerates the JSON.
        print("watchdog recoveries (ms):", [round(r["rebind_ms"], 2) for r in evidence["recoveries"]],
              "wake-to-service (ms):", [round(r["watchdog_ms"], 2) for r in evidence["recoveries"]])


def test_the_watchdog_retries_a_failed_rebind_instead_of_dying_with_it():
    """A watchdog that exits on the first failure converts a recoverable listener into a permanent outage.

    The full suite used to print `RuntimeError('the proxy could not rebind')` from the watchdog thread and carry on, so
    this is not hypothetical: the thread was dying and nothing could bring the listener back afterwards.
    """
    with ProxyThread(_table(), supervise_ms=10.0) as proxy:
        port = proxy.port
        loop = proxy._loop
        assert loop is not None
        server = proxy.server
        assert server is not None
        real_restart = proxy._restart
        attempts = {"count": 0}

        def flaky(loop_, timeout):
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise RuntimeError("the port went to somebody else")
            return real_restart(loop_, timeout)

        proxy._restart = flaky                                        # type: ignore[method-assign]
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        assert _wait_for_restore(proxy, server, time.monotonic() + 5.0), \
            "the watchdog gave up after a failed attempt instead of retrying"
        evidence = proxy.watchdog_stats()
        assert len(evidence["errors"]) == 2, f"failed attempts must be recorded, not swallowed: {evidence['errors']}"
        assert all("the port went to somebody else" in e["error"] for e in evidence["errors"])
        assert evidence["alive"]
        assert proxy.port == port
        assert len(evidence["recoveries"]) == 1 and evidence["recoveries"][0]["rebind_ms"] >= 0.0
