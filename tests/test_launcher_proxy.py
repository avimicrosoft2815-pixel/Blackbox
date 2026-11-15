"""P3B-03, second half: `blackbox run` starts the proxy, and the child's environment is the plan.

Every test here spawns a real child that prints its own environment, because the promise is about what the *child*
sees, not about what the launcher intended. The child then makes one HTTP request through the base URL it was given, so
the second half of the promise — that the listener was up before the child was, and understood the route it was handed
— is observable too.

The upstream in these tests is a local origin, passed in as a route table: the provider defaults are exercised where
they belong (`tests/test_proxy_routes.py`), and nothing here wants to reach the internet.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.routes import Route, Routes
from blackbox.rec import launcher

posix = pytest.mark.skipif(os.name != "posix", reason="the launcher's process-level tests are POSIX-only")
pytestmark = posix

BODY = b'{"ok":true}'


class _Origin(BaseHTTPRequestHandler):
    """The smallest origin that can say it was reached, and with which path."""

    seen: ClassVar[list[dict[str, str]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        type(self).seen.append({"path": self.path, "host": self.headers.get("host", "")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = do_POST

    def log_message(self, *args: object) -> None:                 # silence: the test's own failures are enough
        pass


@pytest.fixture
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Origin.seen = []
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


CHILD = r"""
import json, os, sys, urllib.request
names = ["OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OLLAMA_HOST", "GOOGLE_GEMINI_BASE_URL",
         "BLACKBOX_PROXY", "BLACKBOX_PROXY_URL", "BLACKBOX_RUN_ID"]
out = {name: os.environ.get(name) for name in names}
try:
    request = urllib.request.Request(out["OPENAI_BASE_URL"] + "/chat/completions", data=b"{}", method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        out["status"] = response.status
        out["body"] = response.read().decode()
except Exception as exc:
    out["error"] = "{0}: {1}".format(type(exc).__name__, exc)
print("CHILD " + json.dumps(out), flush=True)
"""


def _child(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    return [sys.executable, str(script)], script


def _printed(capfd: pytest.CaptureFixture[str]) -> dict[str, str]:
    text = capfd.readouterr().out
    line = next(line for line in text.splitlines() if line.startswith("CHILD "))
    return json.loads(line[len("CHILD "):])


def _routes(origin_port: int) -> Routes:
    return Routes([Route(name="openai", base=f"http://127.0.0.1:{origin_port}", prefix="/openai"),
                   Route(name="anthropic", base=f"http://127.0.0.1:{origin_port}", prefix="/anthropic")],
                  default="openai")


def test_the_child_is_pointed_at_the_proxy_and_its_request_arrives_at_the_upstream(tmp_path, origin, capfd):
    argv, _script = _child(tmp_path)
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin), env=env)
    printed = _printed(capfd)
    assert printed["OPENAI_BASE_URL"] == f"{result.proxy['url']}/openai/v1"
    assert printed["ANTHROPIC_BASE_URL"] == f"{result.proxy['url']}/anthropic"
    assert printed["BLACKBOX_PROXY"] == "1"
    assert printed["BLACKBOX_PROXY_URL"] == result.proxy["url"]
    assert printed["BLACKBOX_RUN_ID"] == result.run_id
    assert printed["status"] == 200 and printed["body"] == BODY.decode(), printed
    assert _Origin.seen and _Origin.seen[0]["path"] == "/v1/chat/completions", "the prefix was stripped"
    assert result.proxy["stats"]["requests"] == 1
    assert result.proxy["port"] == int(result.proxy["url"].rsplit(":", 1)[1])


def test_a_variable_the_user_already_set_is_handed_to_the_child_untouched(tmp_path, origin, capfd):
    argv, _script = _child(tmp_path)
    theirs = f"http://127.0.0.1:{origin}"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "OPENAI_BASE_URL": theirs}
    log = tmp_path / "log.txt"
    with open(log, "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin), env=env)
    printed = _printed(capfd)
    assert printed["OPENAI_BASE_URL"] == theirs, "the recorder must not move the user's traffic"
    assert result.proxy["adopted"] == {"OPENAI_BASE_URL": theirs}
    assert "OPENAI_BASE_URL" not in result.proxy["exports"]
    assert printed["status"] == 200, "the child's own URL still works; it simply is not recording"
    assert "openai-own" in result.proxy["stats"].get("per_route", {}) or result.proxy["stats"]["requests"] == 0, \
        "nothing was pointed at the proxy for that variable, so the proxy saw nothing from it"


def test_without_the_proxy_no_listener_is_started_and_no_variable_is_touched(tmp_path, capfd):
    argv, _script = _child(tmp_path)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    log = tmp_path / "log.txt"
    with open(log, "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, env=env)
    printed = _printed(capfd)
    assert result.proxy == {}
    assert printed["OPENAI_BASE_URL"] is None
    assert printed["BLACKBOX_PROXY"] is None
    assert "error" in printed, "with no base URL the child cannot call anything, which is the point"


def test_the_listener_is_gone_when_the_child_is(tmp_path, origin):
    argv, _script = _child(tmp_path)
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin),
                                 env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")})
    url = result.proxy["url"]
    port = int(url.rsplit(":", 1)[1])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                time.sleep(0.05)
        except OSError:
            return
    raise AssertionError(f"the proxy on {port} is still listening after the run ended")


def test_the_summary_line_says_the_proxy_ran(tmp_path, origin):
    argv, _script = _child(tmp_path)
    log = tmp_path / "log.txt"
    with open(log, "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin),
                                 env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")})
    text = log.read_text()
    assert f"proxy {result.proxy['url']}" in text
    assert "5 vars exported" in text, text
