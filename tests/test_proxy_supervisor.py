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


def test_a_rebind_keeps_the_port_the_child_was_told_and_lands_inside_the_budget():
    with ProxyThread(_table()) as proxy:
        port = proxy.port
        elapsed = proxy.rebind()
        assert proxy.port == port, "the child was told this port; a rebind that changes it is a broken child"
        assert elapsed <= REBIND_BUDGET_MS, f"rebind took {elapsed:.1f} ms, budget {REBIND_BUDGET_MS} ms"
        assert proxy.rebinds == [elapsed]


def test_the_watchdog_brings_a_dead_listener_back_without_being_asked():
    with ProxyThread(_table(), supervise_ms=10.0) as proxy:
        server = proxy.server
        assert server is not None and proxy._loop is not None
        port = proxy.port
        # close the listener behind the supervisor's back: exactly the failure it exists for
        import asyncio

        asyncio.run_coroutine_threadsafe(server.close(), proxy._loop).result(5)
        assert not server.is_serving
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not proxy.server.is_serving:      # type: ignore[union-attr]
            time.sleep(0.01)
        assert proxy.server is not None and proxy.server.is_serving, "the watchdog did not restore service"
        assert proxy.port == port, "and it restored it on the same port"
        assert proxy.rebinds and max(proxy.rebinds) <= REBIND_BUDGET_MS
