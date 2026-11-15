"""P3B-09, the half a table cannot carry: the attach rows that can be checked here, checked here.

`docs/attach.md` promises one thing per row — *a verified version and date, or the word "unverified"*. A version number
is worthless if nobody ran the client, so this file runs the ones this sandbox has: the two Python SDKs at the versions
installed in the CI matrix (read from the package, never typed into the doc by hand), `node`'s built-in `fetch`, and
`curl`. Each client is pointed at the proxy **through the variable the launcher exports** (`plan_exports`, the same call
`blackbox run` makes), builds its request with its own code, and the assertion is on what the **origin** received — a
path and a body the client chose, not one the test wrote.

What this does not prove, and the doc says so: that the hosted providers accept the request. There are no keys here and
no route off this sandbox, so every row below is *"this client honours the base URL, on this version, today"* — which is
exactly the property attach depends on, and nothing more.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.supervisor import ProxyThread, plan_exports

CHAT = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
           "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
           "usage": {"input_tokens": 1, "output_tokens": 1}}


class _Handler(http.server.BaseHTTPRequestHandler):
    """A provider-shaped origin: it answers the two paths the SDKs choose, and records everything."""

    protocol_version = "HTTP/1.1"
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).requests.append({"path": self.path, "body": body,
                                    "headers": {k.lower(): v for k, v in self.headers.items()}})
        payload = MESSAGE if self.path.endswith("/messages") else CHAT
        blob = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args: Any) -> None:                    # a silent origin keeps pytest output readable
        return


class Attached:
    """An origin and the proxy in front of it, both on loopback, with the launcher's own export plan in between."""

    def __enter__(self) -> Attached:
        _Handler.requests = []
        self.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.origin.daemon_threads = True
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.origin.server_address[1]}"   # the provider root, as upstreams.json has it
        routes = Routes([Route(name="openai", base=base, prefix="/openai"),
                         Route(name="anthropic", base=base, prefix="/anthropic")], default="openai")
        self.proxy = ProxyThread(routes, supervise_ms=0).start()
        self.plan = plan_exports({}, f"http://127.0.0.1:{self.proxy.port}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.proxy.stop()
        self.origin.shutdown()
        self.origin.server_close()

    @property
    def seen(self) -> list[dict[str, Any]]:
        return _Handler.requests


def test_the_openai_python_sdk_honours_the_variable_the_launcher_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    openai = pytest.importorskip("openai")
    with Attached() as attached:
        monkeypatch.setenv("OPENAI_BASE_URL", attached.plan.exports["OPENAI_BASE_URL"])
        client = openai.OpenAI(api_key="sk-test")                 # no base_url argument: the environment is the seam
        reply = client.chat.completions.create(
            model="gpt-x", messages=[{"role": "user", "content": "hello"}])
        assert reply.choices[0].message.content == "hi"
        seen = attached.seen[-1]
        assert seen["path"] == "/v1/chat/completions", "the SDK appended its own path to the base URL we exported"
        assert json.loads(seen["body"])["model"] == "gpt-x"
        assert openai.__version__  # the doc's version cell is read from here, not typed


def test_the_anthropic_python_sdk_honours_the_variable_the_launcher_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    anthropic = pytest.importorskip("anthropic")
    with Attached() as attached:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", attached.plan.exports["ANTHROPIC_BASE_URL"])
        client = anthropic.Anthropic(api_key="sk-ant-test")
        reply = client.messages.create(model="claude-x", max_tokens=8,
                                       messages=[{"role": "user", "content": "hello"}])
        assert reply.content[0].text == "hi"
        seen = attached.seen[-1]
        assert seen["path"] == "/v1/messages"
        assert json.loads(seen["body"])["model"] == "claude-x"
        assert anthropic.__version__


def test_a_node_script_using_builtin_fetch_reaches_the_same_proxy(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node here (the doc says so rather than claiming the row)")
    script = tmp_path / "attach.mjs"
    script.write_text(
        "const base = process.env.OPENAI_BASE_URL;\n"
        "const r = await fetch(`${base}/chat/completions`, {method: 'POST',\n"
        "  headers: {'content-type': 'application/json'},\n"
        "  body: JSON.stringify({model: 'gpt-x', messages: [{role: 'user', content: 'hello'}]})});\n"
        "const j = await r.json();\n"
        "console.log(JSON.stringify({status: r.status, text: j.choices[0].message.content}));\n",
        encoding="utf-8")
    with Attached() as attached:
        env = {**os.environ, "OPENAI_BASE_URL": attached.plan.exports["OPENAI_BASE_URL"]}
        done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, env=env)
        assert done.returncode == 0, done.stderr[-500:]
        assert json.loads(done.stdout.strip().splitlines()[-1]) == {"status": 200, "text": "hi"}
        assert attached.seen[-1]["path"] == "/v1/chat/completions"
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30)
        assert version.stdout.strip().startswith("v")


def test_curl_reaches_the_same_proxy_with_no_client_library_at_all() -> None:
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("no curl here")
    with Attached() as attached:
        done = subprocess.run(
            [curl, "-sS", "--max-time", "30", "-X", "POST",
             f"{attached.plan.exports['OPENAI_BASE_URL']}/chat/completions",
             "-H", "content-type: application/json",
             "-d", json.dumps({"model": "gpt-x", "messages": []})],
            capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-400:]
        assert json.loads(done.stdout)["id"] == "chatcmpl-1"
        assert attached.seen[-1]["path"] == "/v1/chat/completions"
        assert subprocess.run([curl, "--version"], capture_output=True, text=True, timeout=30).returncode == 0


# ── the row that was wrong until an SDK was actually run (defect 39) ─────────────────────────────────────────────
# The path each client appends to whatever base URL it is given, and the path the provider documents. The two columns
# together are the contract: a base URL is only usable if `export + appended == documented`.
APPENDED = {"openai": ("OPENAI_BASE_URL", "/chat/completions", "/v1/chat/completions"),
            "anthropic": ("ANTHROPIC_BASE_URL", "/v1/messages", "/v1/messages"),
            "gemini": ("GOOGLE_GEMINI_BASE_URL", "/v1beta/models/m:generateContent",
                       "/v1beta/models/m:generateContent")}


@pytest.mark.parametrize("name", sorted(APPENDED))
def test_the_exported_base_plus_the_clients_own_path_is_the_documented_provider_path(name: str) -> None:
    """What `blackbox run` exports, routed through the default upstream for that name, lands where the provider serves."""
    from urllib.parse import urlsplit

    var, appended, documented = APPENDED[name]
    route = Routes.provider_defaults(names=(name,), default=name).route(name)
    exported = urlsplit(plan_exports({}, "http://127.0.0.1:8787").exports[var]).path.rstrip("/")
    assert route.target_path(exported + appended) == documented, (
        f"{var}={exported} + {appended} does not reach {documented} on {route.base}")
