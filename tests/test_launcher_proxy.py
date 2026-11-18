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

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.proxy.routes import Route, Routes
from blackbox.rec import launcher
from blackbox.tape import frame as frame_mod
from blackbox.tape import scanner

posix = pytest.mark.skipif(os.name != "posix", reason="the launcher's process-level tests are POSIX-only")
pytestmark = posix

BODY = b'{"ok":true}'


class _Origin(BaseHTTPRequestHandler):
    """The smallest origin that can say it was reached, and with which path."""

    seen: ClassVar[list[dict[str, str]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        # the headers are kept so a test can assert what did *not* arrive here: our own `x-blackbox-*` names
        type(self).seen.append({"path": self.path, "host": self.headers.get("host", ""),
                                "headers": {name.lower(): value for name, value in self.headers.items()}})
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


def test_the_proxy_vantage_lands_on_the_launchers_tape(tmp_path, origin, capfd):
    """A request that passes through the proxy must be *recorded*, not merely forwarded.

    Without a receiver the listener relays bytes and writes nothing, so the capsule came back with `events: 5` — the
    bootstrap frames and no request — while `proxy.stats.requests` said 1. The traffic was invisible in the artefact the
    user would read, which is the one failure this recorder exists to prevent.
    """
    argv, _script = _child(tmp_path)
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                        proxy_routes=_routes(origin), env=env)
    assert _printed(capfd)["status"] == 200
    kinds: set[str] = set()
    for path in (tmp_path / "data" / "capsules").glob("*.bbx"):
        raw = cap.read_capsule(str(path)).frames_of(1)
        off = C.SEGMENT_HEADER_BYTES
        while off < len(raw):
            try:                                 # the segment is padded after the last frame; padding is not a frame
                frame, off = frame_mod.decode(raw, off)
            except Exception:
                break
            kinds.add(C.KIND_NAMES[frame["kind"]])
    assert {"MODEL_REQUEST", "MODEL_CHUNK", "MODEL_RESPONSE"} <= kinds, sorted(kinds)


def test_a_completed_proxied_call_is_not_reported_as_an_interrupted_node(tmp_path, origin, capfd):
    """The counts a diagnosis reads: `nodes_interrupted` must not claim a crash that did not happen."""
    argv, _script = _child(tmp_path)
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                        proxy_routes=_routes(origin), env=env)
    assert _printed(capfd)["status"] == 200
    counts = [cap.read_capsule(str(p)).manifest["counts"]
              for p in (tmp_path / "data" / "capsules").glob("*.bbx")]
    recorded = [c for c in counts if c["events"] > 5]
    assert recorded, "the tape with the request on it is the one these counts describe"
    assert all(c["nodes_interrupted"] == 0 for c in counts), counts


# ── D-053: per-call correlation, measured through a real child, a real proxy and a real origin ────────────────────
CONCURRENT_CHILD = r"""
import asyncio, os, httpx
from blackbox import api

BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


async def one(name):
    async with httpx.AsyncClient(timeout=10) as client:
        with api.run(name):
            response = await client.post(BASE + "/chat/completions", json=PAYLOAD)
            print("CHILD", name, response.status_code, flush=True)


async def main():
    await asyncio.gather(one("first"), one("second"))


asyncio.run(main())
"""


def _calls_on_tape(data_dir: Path) -> list[dict]:
    """Every `MODEL_REQUEST` payload in the data directory, with the vantage that wrote it."""
    out: list[dict] = []
    for tape in sorted((data_dir / "tape").iterdir()):
        scan = scanner.scan(str(data_dir), tape.name)
        for frame in scan.frames:
            if frame["kind"] == C.KIND_MODEL_REQUEST:
                out.append({**json.loads(frame["payload"]), "vantage": scan.meta.get("vantage"),
                            "node_slot": frame.get("node_slot")})
    return out


def _run_starts(data_dir: Path) -> list[dict]:
    """Every `RUN_START` payload in the data directory — the identity a tape declares for itself."""
    from blackbox.codec import cbor

    out: list[dict] = []
    for tape in sorted((data_dir / "tape").iterdir()):
        scan = scanner.scan(str(data_dir), tape.name)
        for frame in scan.frames:
            if frame["kind"] == C.KIND_RUN_START:
                out.append(cbor.decode_exact(frame["payload"]))
    return out


def _graphs(data_dir: Path) -> list[dict]:
    """Every `graph.json` the run left, one per tape that recorded frames."""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((data_dir / "runs").glob("*/graph.json"))]


def _app_graph(data_dir: Path, names: set[str]) -> dict:
    """The graph of the tape whose nodes are the app's — the proxy's tape carries a `proxy_call` span instead."""
    for graph in _graphs(data_dir):
        if {node["name"] for node in graph.get("nodes", [])} >= names:
            return graph
    raise AssertionError(f"no graph holds the nodes {sorted(names)}: {[g.get('nodes') for g in _graphs(data_dir)]}")


def test_the_call_id_lands_on_both_tapes_and_never_reaches_the_upstream(tmp_path, origin, capfd):
    """One request, one id, two records — and the id is ours, so the origin must not see it."""
    script = tmp_path / "child.py"
    script.write_text(CONCURRENT_CHILD.replace("asyncio.gather(one(\"first\"), one(\"second\"))", "one(\"solo\")"),
                      encoding="utf-8")
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        result = launcher.launch([sys.executable, str(script)], data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=env)
    assert result.exit_code == 0, result.exit_code
    calls = _calls_on_tape(tmp_path / "data")
    assert len(calls) == 2, f"the SDK writes one record and the proxy writes one: {calls}"
    ids = {call["call_id"] for call in calls}
    assert len(ids) == 1 and None not in ids, calls
    proxy_call = next(call for call in calls if call["linked_run"] == result.run_id)
    assert proxy_call["call_id"] == ids.pop(), "both sides carry one id for one call"
    assert _Origin.seen and _Origin.seen[0]["path"] == "/v1/chat/completions"
    assert not any(name.lower().startswith("x-blackbox-") for name in _Origin.seen[0].get("headers", {})), \
        "the call id is ours: it must be stripped before the upstream"


def test_two_concurrent_calls_get_two_ids_and_two_graph_edges_each_naming_its_own_node(tmp_path, origin, capfd):
    """The acceptance case for D-053: concurrency, two nodes, and no crossing over in the graph."""
    script = tmp_path / "child.py"
    script.write_text(CONCURRENT_CHILD, encoding="utf-8")
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        result = launcher.launch([sys.executable, str(script)], data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=env)
    assert result.exit_code == 0, result.exit_code
    graph = _app_graph(tmp_path / "data", {"first", "second"})
    nodes = {node["name"]: node["node_slot"] for node in graph["nodes"]}
    assert sorted(key for key in nodes if key in {"first", "second"}) == ["first", "second"], graph["nodes"]
    app_nodes = {node["name"]: node for node in graph["nodes"] if node["name"] in {"first", "second"}}
    assert {node["kind"] for node in app_nodes.values()} == {"run"}, app_nodes

    # B4's measurement, from a real launch: one id per call, and the proxy's recorded span on the other end of it, so
    # the edge is node-to-node — both ends recorded by their own writer, neither invented.
    call_or_node = [edge for edge in graph["edges"] if edge["level"] in ("call", "node")]
    assert len(call_or_node) == 2, graph["edges"]
    assert len({edge["from"]["call_id"] for edge in call_or_node}) == 2, "two calls, two ids"
    assert {edge["level"] for edge in call_or_node} == {"node"}, call_or_node
    assert sorted(edge["nodes"]["from"] for edge in call_or_node) == sorted(
        nodes[name] for name in ("first", "second")), "each edge names the node its own call was inside"
    to_nodes = [edge["nodes"]["to"] for edge in call_or_node]
    assert all(isinstance(node, int) and node >= C.NODE_SLOT_PROXY_BASE for node in to_nodes), to_nodes
    assert len(set(to_nodes)) == 2, "two calls in flight at once got two proxy spans, not one shared span"
    assert graph["edge_summary"]["call_level"] == 0 and graph["edge_summary"]["node_level"] == 2
    assert graph["edge_summary"]["node_join"] == "exact" and graph["edge_summary"]["ambiguous"] == 0
    assert all(edge["node_join"] == "exact" for edge in call_or_node), call_or_node

    # The proxy's spans are nodes of the proxy's own tape. A run may leave more than one graph (one per run id the
    # child started), so the spans are counted across all of them rather than assumed to live in one file.
    everything = [node for other in _graphs(tmp_path / "data") for node in other["nodes"]]
    spans = [node for node in everything if node["kind"] == "proxy_call"]
    assert len(spans) == 2, everything
    assert {node["owner"] for node in spans} == {"proxy"}, spans
    assert {node["name"] for node in spans} == {"POST /v1/chat/completions"}, spans
    used_as_an_end = {edge["nodes"]["to"] for other in _graphs(tmp_path / "data") for edge in other["edges"]
                      if edge["level"] == "node"}
    assert {node["node_slot"] for node in spans} <= used_as_an_end, (used_as_an_end, spans)


def test_an_uninstrumented_child_leaves_no_node_and_no_call_edge(tmp_path, origin, capfd):
    """Missing instrumentation is a *fact about the run*, not a gap to paper over (docs/node-edges.md).

    The child here is plain `urllib`: no SDK, no seam, no `x-blackbox-call` on the wire. The proxy still records the
    request — that is its job and its vantage — but it must record it with `call_id: null`, and the run's graph must
    have no node and no call edge. A recorder that manufactured an id here would give the graph an edge whose other end
    it had invented, which is the one thing D-052 forbids.
    """
    argv, _script = _child(tmp_path)
    log = tmp_path / "log.txt"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    with open(log, "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=env)
    assert result.exit_code == 0, result.exit_code
    calls = _calls_on_tape(tmp_path / "data")
    assert len(calls) == 1, f"one request, one record: {calls}"
    assert calls[0]["call_id"] is None, "an uninstrumented child was never given an id, so none may be recorded"
    assert calls[0]["linked_run"] is None, "no header arrived, so the record must not claim a run it was not told"
    # The record is still the run's own: the launcher's tape writes it, and that tape's `RUN_START` names the run.
    starts = _run_starts(tmp_path / "data")
    assert starts and {start["run_id"] for start in starts} == {result.run_id}, starts

    graphs = _graphs(tmp_path / "data")
    assert graphs, "the run left a graph"
    # The proxy did forward the request, so its tape records a span of its own — that span is a fact about the proxy's
    # vantage, not an app node. What must be absent is an app node, and any edge that would join one: `call_id` is null
    # on the record, so there is nothing to join on, and inventing an end is what D-052 forbids.
    spans = [node for graph in graphs for node in graph["nodes"]]
    assert [(node["kind"], node["owner"]) for node in spans] in ([], [("proxy_call", "proxy")]), spans
    for graph in graphs:
        for edge in graph["edges"]:
            assert edge["level"] == "run", edge
            assert edge["nodes"] == {"from": None, "to": None}, edge
        assert graph["edge_summary"]["call_level"] == 0
        assert graph["edge_summary"]["node_level"] == 0
        assert graph["edge_summary"]["node_join"] == "unknown", graph["edge_summary"]
