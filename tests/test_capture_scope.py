"""P0: a seam patches every client in the process, so capture needs an explicit scope (D-026).

The reviewer's audit found the same shape of problem as the fail-open defect: the seam was doing exactly what it was
told and capturing more than it was allowed to. Seam A patches `Client.__init__` everywhere; a process that also calls
a webhook, a vector store or an internal service would have had all of it scooped into the tape.

These tests hold the line in both directions:

* unrelated hosts are **not captured**, and the traffic behaves exactly as it does without the seam (same body, same
  exception) — a scope that changed behaviour would be worse than no scope;
* the skip is **counted**, so a capsule can distinguish "nothing was captured because nothing model-shaped happened"
  from "the recorder was not looking at that host". An absence is not evidence of absence.
"""
from __future__ import annotations

import os

import pytest

from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.scope import DEFAULT_HOSTS, ENV_VAR, CaptureScope

httpx = pytest.importorskip("httpx")

PAYLOAD = {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


# ── the type itself ────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_default_scope_is_the_providers_we_support_plus_loopback():
    scope = CaptureScope.default()
    assert scope.allows("https://api.openai.com:443/v1/chat/completions")
    assert scope.allows("api.anthropic.com")
    assert scope.allows("http://127.0.0.1:8123/v1/chat/completions"), "the mock provider and local proxies live here"
    assert scope.allows("localhost:9000")
    assert not scope.allows("https://hooks.example.com/publish")
    assert not scope.allows("https://api.otherprovider.example/v1/chat")
    assert scope.source == "default"


def test_a_host_is_compared_without_its_port_or_scheme():
    scope = CaptureScope(hosts=("API.OpenAI.com",))
    assert scope.allows("https://api.openai.com/v1/chat")
    assert scope.allows("http://API.OPENAI.COM:8080/v1/chat")
    assert scope.allows("[::1]:9000") is False            # not in this scope, but the parse must not crash


def test_a_wildcard_matches_subdomains_and_not_the_bare_domain():
    """`*.example.com` must not quietly mean `example.com`: a wildcard is a grant the operator wrote deliberately."""
    scope = CaptureScope(hosts=("*.example.com",))
    assert scope.allows("api.example.com")
    assert scope.allows("a.b.example.com")
    assert not scope.allows("example.com")
    assert not scope.allows("notexample.com")


def test_a_star_is_the_only_way_to_capture_everything_and_it_says_so():
    scope = CaptureScope.from_config(["*"])
    assert scope.allows("anything.at.all")
    assert scope.source == "wildcard"


def test_the_environment_can_add_hosts_and_is_read_in_order(monkeypatch):
    monkeypatch.setenv(ENV_VAR, " gateway.internal , *.azure-api.net ")
    scope = CaptureScope.from_config()
    assert scope.hosts == ("gateway.internal", "*.azure-api.net")
    assert scope.source == "environment"
    assert scope.allows("myinstance.azure-api.net")
    monkeypatch.delenv(ENV_VAR)
    assert CaptureScope.from_config().hosts == DEFAULT_HOSTS


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "from-env.example")
    scope = CaptureScope.from_config(["from-argument.example"])
    assert scope.hosts == ("from-argument.example",)
    assert scope.source == "argument"


# ── the seams honour it ───────────────────────────────────────────────────────────────────────────────────────────


def _stream(client: httpx.Client) -> bytes:
    with client.stream("POST", "/v1/chat/completions", json=PAYLOAD) as response:
        return b"".join(response.iter_bytes())


def test_seam_a_does_not_record_an_unrelated_host_and_leaves_it_untouched():
    from blackbox.cap_sdk.seam_httpx_hooks import install

    recorder = Recorder()
    scope = CaptureScope(hosts=("api.openai.com",))       # loopback deliberately excluded
    handle = install(recorder, scope=scope)
    try:
        with MockProvider(Scenario(chunks=5)) as provider, \
                httpx.Client(base_url=provider.base_url, timeout=10) as client:
            captured_nothing = _stream(client)
        bare_recorder = Recorder()
        with MockProvider(Scenario(chunks=5)) as provider, \
                httpx.Client(base_url=provider.base_url, timeout=10) as client:
            expected = _stream(client)
    finally:
        handle.uninstall()

    assert recorder.calls == [], "a host outside the scope was captured anyway"
    assert captured_nothing == expected, "the scope changed what the application received"
    assert scope.stats["out_of_scope"] >= 1, "the skip must be counted, or the report cannot say it was not looking"
    assert b"token" in captured_nothing
    assert bare_recorder.calls == []


def test_seam_a_records_loopback_when_the_scope_allows_it():
    from blackbox.cap_sdk.seam_httpx_hooks import install

    recorder = Recorder()
    handle = install(recorder)                            # the default scope includes loopback
    try:
        with MockProvider(Scenario(chunks=4)) as provider, \
                httpx.Client(base_url=provider.base_url, timeout=10) as client:
            _stream(client)
    finally:
        handle.uninstall()

    assert len(recorder.calls) == 1
    assert recorder.calls[0].chunks >= 1


def test_seam_b_does_not_record_an_unrelated_host():
    from blackbox.cap_sdk.seam_httpx_transport import install

    recorder = Recorder()
    scope = CaptureScope(hosts=("api.openai.com",))
    with MockProvider(Scenario(chunks=4)) as provider:
        client = httpx.Client(base_url=provider.base_url, timeout=10)
        handle = install(recorder, client=client, scope=scope)
        try:
            body = _stream(client)
        finally:
            handle.uninstall()
            client.close()

    assert recorder.calls == []
    assert scope.stats["out_of_scope"] >= 1
    assert b"token" in body


def test_unrelated_traffic_fails_the_same_way_with_the_seam_installed():
    """The scope must not turn an unreachable host into a recorder error, or a connection failure nobody could
    explain would appear in an application that only added a recorder."""
    from blackbox.cap_sdk.seam_httpx_hooks import install

    def attempt() -> str:
        try:
            httpx.get("http://capture-scope.invalid/nothing", timeout=2)
            return "ok"
        except Exception as exc:
            return type(exc).__name__

    bare = attempt()
    recorder = Recorder()
    handle = install(recorder)
    try:
        seamed = attempt()
    finally:
        handle.uninstall()

    assert seamed == bare, f"the seam changed how an unrelated failure surfaced: {bare} vs {seamed}"
    assert recorder.calls == []


def test_the_scope_is_written_into_the_seam_decision():
    """A reader of the capsule has to be able to ask what the recorder was allowed to look at."""
    from blackbox.cap_sdk.seam_httpx_hooks import install

    handle = install(Recorder(), scope=CaptureScope(hosts=("gateway.internal", "*.azure-api.net")))
    try:
        scope = handle.detail["scope"]
        assert scope["hosts"] == ["gateway.internal", "*.azure-api.net"]
        assert scope["source"] == "argument"
        assert "out_of_scope" in scope["stats"]
    finally:
        handle.uninstall()


def test_a_seam_installed_without_a_scope_reads_the_environment(monkeypatch):
    from blackbox.cap_sdk.seam_httpx_hooks import install

    monkeypatch.setenv(ENV_VAR, "only-this.example")
    handle = install(Recorder())
    try:
        assert handle.detail["scope"]["hosts"] == ["only-this.example"]
        assert handle.detail["scope"]["source"] == "environment"
    finally:
        handle.uninstall()
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert os.environ.get(ENV_VAR) is None
