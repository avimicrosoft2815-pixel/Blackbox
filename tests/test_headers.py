"""P3B-04's first half: `x-blackbox-run` is injected only behind a proxy, and never reaches an upstream.

The header is the only thing in this product that modifies someone else's request, so the tests are about the two ways it
could be wrong rather than about the happy path:

* **injected when it must not be.** An app that was never pointed at a loopback proxy sends no header at all — asserted
  against a mock provider that pretends to be a real one, because "the header is harmless" is the argument that produces
  the leak R16 names.
* **kept when it should be stripped.** The proxy removes every `x-blackbox-*` header before forwarding, so the mock
  provider sees nothing of ours even when the value was set by hand.

The middle test is the one that makes the feature real: with `BLACKBOX_PROXY=1`, the same SDK call arrives at the proxy
carrying the header, and the proxy records the run id on the call — which is the whole mechanism an `exact` link rests on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from blackbox.cap_sdk import headers
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer


class _Recorder:
    """The smallest receiver that records what the proxy opened, so the test can read the linked run off the call."""

    def __init__(self) -> None:
        self.opened: list[Any] = []

    def open_call(self, provider: str, path: str, request: Any, **kwargs: Any) -> Any:
        from blackbox.cap_sdk.base import ModelCall

        call = ModelCall(provider=provider, path=path, linked_run=kwargs.get("linked_run"))
        self.opened.append(call)
        return call

    def receive(self, call: Any, chunk: bytes, **_kwargs: Any) -> None:
        pass

    def close_call(self, call: Any, **_kwargs: Any) -> None:
        pass


# ── the policy, without a socket ────────────────────────────────────────────────────────────────────────────────────

def test_no_header_unless_the_launcher_said_a_proxy_is_in_front_of_us():
    plain: dict[str, str] = {}
    assert headers.inject(plain, {}) is False
    assert plain == {}, "an unproxied process must not touch its own request headers"

    for value in ("", "0", "true", "yes", "2"):
        env = {headers.ENV_PROXY: value, headers.ENV_RUN: "run-1"}
        assert headers.inject(plain, env) is False, value
        assert plain == {}


def test_the_header_carries_the_run_and_the_node():
    env = {headers.ENV_PROXY: "1", headers.ENV_RUN: "run-abc"}
    plain: dict[str, str] = {}
    assert headers.inject(plain, env) is True
    assert plain[headers.HEADER] == "run-abc"
    assert headers.inject(plain, env, node=7) is True
    assert plain[headers.HEADER] == "run-abc/7"
    assert headers.parse(plain[headers.HEADER]) == ("run-abc", 7)
    assert headers.parse("run-abc") == ("run-abc", None), "a value without a node still names its run"
    assert headers.parse(None) == (None, None)


def test_a_proxy_variable_without_a_run_id_injects_nothing():
    """Half a header is worse than none: it would link every call of an unnamed run to whatever parsed it."""
    plain: dict[str, str] = {}
    assert headers.inject(plain, {headers.ENV_PROXY: "1"}) is False
    assert plain == {}


def test_strip_removes_every_name_of_ours_and_leaves_the_rest():
    values = {"authorization": "Bearer x", "x-blackbox-run": "r/1", "X-BlackBox-Error": "leak", "content-type": "a"}
    removed = headers.strip(values)
    assert sorted(removed) == ["x-blackbox-error", "x-blackbox-run"]
    assert values == {"authorization": "Bearer x", "content-type": "a"}


# ── against a mock provider that pretends to be the real one ────────────────────────────────────────────────────────

def test_an_unproxied_sdk_request_carries_nothing_of_ours():
    provider = MockProvider(Scenario(model="mock-1", chunks=2)).start()
    try:
        import httpx

        with httpx.Client() as client:
            response = client.post(f"http://127.0.0.1:{provider.port}/v1/chat/completions",
                                   json={"model": "mock-1", "messages": []})
            assert response.status_code == 200
        seen = provider.received[-1]["headers"]
        assert not any(name.lower().startswith("x-blackbox-") for name in seen), \
            f"a header of ours reached the provider: {sorted(seen)}"
    finally:
        provider.stop()


def test_the_seam_injects_only_when_the_launcher_says_a_proxy_is_in_front(monkeypatch):
    """End to end through seam A: the same SDK call, twice, and the provider's view of the difference."""
    http_clients = pytest.importorskip("httpx")
    from blackbox.cap_sdk import seam_httpx_hooks
    from blackbox.cap_sdk.base import Recorder

    provider = MockProvider(Scenario(model="mock-1", chunks=2)).start()
    recorder = Recorder()
    handle = seam_httpx_hooks.install(recorder)
    try:
        monkeypatch.delenv(headers.ENV_PROXY, raising=False)
        with http_clients.Client() as client:
            client.post(f"{provider.base_url}/v1/chat/completions", json={"model": "mock-1", "messages": []})
        assert not any(name.lower().startswith("x-blackbox-") for name in provider.received[-1]["headers"]), \
            "unproxied: nothing of ours may leave the process"

        monkeypatch.setenv(headers.ENV_PROXY, "1")
        monkeypatch.setenv(headers.ENV_RUN, "run-seam")
        with http_clients.Client() as client:
            client.post(f"{provider.base_url}/v1/chat/completions", json={"model": "mock-1", "messages": []})
        seen = {name.lower(): value for name, value in provider.received[-1]["headers"].items()}
        assert seen[headers.HEADER] == "run-seam"
    finally:
        handle.uninstall()
        provider.stop()


# ── through a real proxy: injected, read for the link, stripped before the upstream ─────────────────────────────────

class _EchoOrigin:
    """A threaded origin that records the headers it was sent. A thread, not the test's event loop."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:                    # http.server's spelling
                length = int(self.headers.get("content-length", "0"))
                if length:
                    self.rfile.read(length)
                seen.append({name.lower(): value for name, value in self.headers.items()})
                body = json.dumps({"id": "chatcmpl-h", "choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.seen = seen
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        import threading

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_the_proxy_reads_the_run_id_and_the_upstream_never_sees_it(tmp_path: Path):
    """The whole mechanism in one test: SDK-side header in, link recorded, header out.

    The proxy runs on its own loop in its own thread and the client is synchronous, which is the shape the rest of the
    proxy tests use — and now for a second reason. The first version of this test put the client, the proxy and the
    origin on one `asyncio.run()` loop; on 3.12 it deadlocked (the loop parked in `select` with nothing runnable) and on
    3.11 and 3.13 it passed, so the harness was measuring an asyncio difference rather than the header. A test that hangs
    on one interpreter is not evidence about the thing it claims to test.
    """
    import asyncio
    import threading

    import httpx

    from blackbox import constants as C
    from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
    from blackbox.tape import scanner
    from blackbox.tape import writer as writer_module

    origin = _EchoOrigin()
    tape = writer_module.TapeWriter(str(tmp_path / "t"), segment_bytes=256 * 1024).open()
    recorder = SinkRecorder(TapeSink(tape.emit, content="full", coalesce_bytes=1))
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict[str, Any] = {}

    def run() -> None:
        asyncio.set_event_loop(loop)

        async def start() -> None:
            routes = Routes([Route(name="openai", base=origin.url, prefix="/v1")], default=None)
            state["proxy"] = await ProxyServer(routes, receiver=recorder).start()
            ready.set()

        loop.run_until_complete(start())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "the proxy did not start"
        proxy = state["proxy"]
        request_headers: dict[str, str] = {}
        env = {headers.ENV_PROXY: "1", headers.ENV_RUN: "run-9"}
        assert headers.inject(request_headers, env, node=3) is True
        with httpx.Client(timeout=15.0) as client:
            response = client.post(f"http://127.0.0.1:{proxy.port}/v1/chat/completions",
                                   json={"model": "m", "messages": []}, headers=request_headers)
            assert response.status_code == 200
        assert not any(name.startswith("x-blackbox-") for name in origin.seen[-1]), \
            "the proxy must strip every header of ours before the upstream"
        assert proxy.stats.requests == 1
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        origin.close()
        tape.publish()
        tape.close()

    frames = scanner.scan(str(tmp_path / "t"), tape.writer_uuid.hex()).frames
    summary = json.loads(next(f["payload"] for f in frames if f["kind"] == C.KIND_MODEL_REQUEST))
    assert summary["linked_run"] == "run-9", "the tape carries the run the header named, which is what links it"
