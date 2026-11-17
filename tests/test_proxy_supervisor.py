"""P3B-03, the half that does not need a child process: the variable plan, and a listener that comes back.

Two promises are tested here, and they are the two that make the proxy safe to hand a launcher:

* **Nothing the child already chose is overwritten.** A base-URL variable that is set stays set, and its destination
  becomes a route — because the alternative is either a recorder that silently redirects traffic or a proxy that
  refuses to reach the destination the user named.
* **A dead listener comes back inside the budget**, on the port the child was told, and the watchdog is what notices.
"""
from __future__ import annotations

import time

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import (
    BASE_URL_VARS,
    ENV_PROXY,
    REBIND_BUDGET_MS,
    SUPERVISE_MS,
    ProxyThread,
    plan_exports,
)


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


def test_a_rebind_keeps_the_port_the_child_was_told_and_lands_inside_the_budget():
    with ProxyThread(_table()) as proxy:
        port = proxy.port
        elapsed = proxy.rebind()
        assert proxy.port == port, "the child was told this port; a rebind that changes it is a broken child"
        assert elapsed <= REBIND_BUDGET_MS, f"rebind took {elapsed:.1f} ms, budget {REBIND_BUDGET_MS} ms"
        assert proxy.rebinds == [elapsed]


def _break_and_wait(proxy: ProxyThread, timeout: float = 5.0) -> dict:
    """Close the listener behind the supervisor's back and time how long the child would have waited.

    The number that matters is the **outage**: from the listener stopping to a new listener serving on the same port.
    Four numbers are returned, and they answer different questions: `outage_ms` is what a child experiences,
    `rebind_ms` is the work inside the rebind, `dispatch_ms` is how long *this test's* request to close took to reach
    the loop thread, and `noticed_ms` is how long the supervisor took to *see* the loss — present only when the
    listener said so (`lost_cb`, defect 58), absent when detection was the tick.

    `outage_ms` starts when `close()` has **returned**, not when it was asked for. Until the coroutine runs, the
    listener is still accepting, so a child sees no outage at all — and charging the test's own cross-thread hop to
    the product is how a 103.2 ms reading appeared on a CI runner whose watchdog and loop threads the OS had both
    descheduled (defect 60: the rebind waited 103.04 ms inside `run_coroutine_threadsafe(...).result()`, with 86.2 ms
    of measured tick lateness in the same run). `dispatch_ms` is reported beside the outage so the harness's share is
    visible and cannot be argued about afterwards. The budget is still asserted on **every** sample.
    """
    import asyncio

    server = proxy.server
    loop = proxy._loop
    assert server is not None and loop is not None
    port_before = proxy.port
    before = len(proxy.rebinds)
    noticed_before = len(proxy.noticed_ms)
    asked = time.monotonic()
    asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
    dispatch_ms = (time.monotonic() - asked) * 1000.0
    started = time.monotonic()                        # the listener is down from here, not from `asked`
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = proxy.server
        if (current is not None and current is not server and current.is_serving
                and len(proxy.rebinds) > before):
            return {"outage_ms": (time.monotonic() - started) * 1000.0,
                    "rebind_ms": proxy.rebinds[before], "dispatch_ms": dispatch_ms, "port_before": port_before,
                    "port_after": proxy.port, "watchdog": True,
                    "noticed_ms": (proxy.noticed_ms[len(proxy.noticed_ms) - 1]
                                   if len(proxy.noticed_ms) > noticed_before else None)}
        time.sleep(0.0005)
    raise AssertionError(f"the watchdog did not restore service within {timeout} s")


def test_the_watchdog_brings_a_dead_listener_back_without_being_asked():
    """Structural, not statistical: the watchdog is what restored service, and it kept the port the child was told."""
    with ProxyThread(_table()) as proxy:
        port = proxy.port
        sample = _break_and_wait(proxy)
        assert proxy.server is not None and proxy.server.is_serving
        assert sample["port_after"] == port, "a rebind that moves the port breaks the child that was told it"
        assert len(proxy.rebinds) == 1, "the rebind that restored service was the watchdog's, not a manual one"
        assert proxy.rebinds == [sample["rebind_ms"]]


def test_every_watchdog_rebind_lands_inside_the_budget_not_the_median_of_them():
    """P3B-03 / §11.8, measured as the requirement is written: **every** watchdog-triggered rebind, per sample.

    The earlier version of this test took the median of five samples, four of which were manual `rebind()` calls. That
    is a statistic about a different experiment: it can pass while the single sample a child would have waited on is
    outside the budget, and it did (defect 40 — one macOS cell read 107.9 ms and the response was to quote a median).
    So: the watchdog alone, the default tick, N breaks, and every sample asserted. The worst sample is printed, so a
    gated row and its outliers are visible in the same failure message.
    """
    samples: list[dict] = []
    with ProxyThread(_table()) as proxy:
        port = proxy.port
        assert proxy._supervise_ms == SUPERVISE_MS, "this test measures the tick that ships"
        tick = SUPERVISE_MS / 1000.0
        # Phase decides how much of the budget the tick spends. A listener that dies just after a tick waits a whole
        # tick; one that dies just before a tick waits almost nothing. Restoration happens *at* a tick, so a 1 ms sleep
        # then a break is the worst case. So both ends are the worst case deliberately, and the six phases between them
        # are evenly spaced rather than random: that covers the space and leaves no seed to argue about.
        delays = [0.001] + [tick * i / 7 for i in range(1, 7)] + [0.001]
        for delay in delays:
            time.sleep(delay)
            samples.append(_break_and_wait(proxy))
        assert all(s["port_after"] == port for s in samples), "every rebind must keep the port"
        assert len(proxy.rebinds) == len(samples), "each break must have been repaired by exactly one watchdog rebind"
    outages = [s["outage_ms"] for s in samples]
    worst = max(outages)
    # A red row has to say *which* half of the outage was ours: the rebind, or a watchdog thread the machine did not
    # schedule. `wake_late_ms` is the supervisor's own record of how late its ticks woke (defect 58), so a starved
    # runner is named as such in the failure message instead of being argued about afterwards.
    lateness = getattr(proxy, "wake_late_ms", [])
    assert worst <= REBIND_BUDGET_MS, (
        f"worst watchdog outage {worst:.1f} ms over the {REBIND_BUDGET_MS} ms budget; "
        f"outages {[round(o, 1) for o in outages]} ms, rebinds {[round(s['rebind_ms'], 2) for s in samples]} ms, "
        f"dispatch {[round(s['dispatch_ms'], 2) for s in samples]} ms, "
        f"restores from the loop {proxy.restores_from_loop}, "
        f"worst watchdog tick lateness {max(lateness, default=0.0):.1f} ms over {len(lateness)} ticks")


def test_a_listener_that_dies_without_saying_so_is_still_restored_on_the_tick():
    """The early notice is an optimisation of detection, not a replacement for the watchdog (defect 58).

    A close says so and is noticed in microseconds; a listener that dies for any other reason — an OS error, a socket
    pulled out from under it, a close before the callback was attached — says nothing, and the tick is the only thing
    left. This test removes the notice and asserts the old behaviour survives, because a fast path that is the *only*
    path is a new way to lose the listener entirely.
    """
    with ProxyThread(_table()) as proxy:
        listener = proxy.server
        assert listener is not None
        listener.lost_cb = None                      # die silently, the way an OS-level loss would
        sample = _break_and_wait(proxy)
        assert sample["noticed_ms"] is None, "nothing notified the supervisor, so nothing may claim an early notice"
        assert proxy.noticed_ms == [], proxy.noticed_ms
    assert sample["outage_ms"] > 0
    assert sample["port_after"] == sample["port_before"], "a backstop rebind keeps the port the child was told"


def test_a_fast_tick_never_moves_the_port_and_never_double_restores() -> None:
    """The race a full-suite run found (defect 60): two owners, one port.

    With the fix in place the notice repairs the listener on the loop *and* wakes the watchdog, so for a moment two
    paths can both see "no listener": the loop's task and the tick. Before `_restore_pending` was set synchronously by
    the notice, the tick could get there first, call `rebind()`, and race the loop for the port — the loser fell back to
    a port of its own, which breaks every child that was told the old one. A 1 ms tick makes that window wide instead of
    theoretical, and 25 breaks are enough for it to happen on a loaded machine if it still can.
    """
    with ProxyThread(_table(), supervise_ms=1.0) as proxy:
        port = proxy.port
        for index in range(25):
            sample = _break_and_wait(proxy)
            assert sample["port_after"] == port, f"break {index} moved the port: {sample}"
        assert not proxy.port_changed, "a rebind gave up the port the child was told"
        assert len(proxy.rebinds) == 25, f"25 breaks, {len(proxy.rebinds)} rebinds: something restored twice"

