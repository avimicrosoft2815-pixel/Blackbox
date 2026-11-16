"""P3B-03's acceptance line, run against real clients: `blackbox run -- node app.js` and `-- python app.py`.

The question the ticket asks is "one run with linked nodes". Answering it from a test that fakes the child answers a
different question, so this script starts a real loopback upstream, writes a real Python client and a real Node client,
runs each one under `blackbox run`, and then reads the capsules back:

* how many capsules the run left, and whether each one passes its own integrity root;
* whether the proxy's vantage is on a tape at all (`MODEL_REQUEST` / `MODEL_CHUNK` / `MODEL_RESPONSE`), and whether the
  manifest counts agree with that (`nodes_interrupted` used to say `1` for a call that had completed);
* whether every capsule names the same run id, and what the `links.json` of each one says.

Everything it reports is measured here and written to `bench/results/`, including the parts that failed: a script that
only records the happy path is a script that cannot report a regression.

    python bench/accept_p3b03.py --out bench/results/2026-11-16-p3b03-acceptance.json

stdlib only, and no network beyond 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.cap import format as cap
from blackbox.constants import (
    KIND_MODEL_CHUNK,
    KIND_MODEL_REQUEST,
    KIND_MODEL_RESPONSE,
    KIND_NAMES,
    SEGMENT_HEADER_BYTES,
)
from blackbox.tape import frame as frame_mod

PY_CLIENT = '''\
import json, os, urllib.error, urllib.request
base = os.environ["OPENAI_BASE_URL"].rstrip("/")
payload = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}).encode()
req = urllib.request.Request(base + "/chat/completions", method="POST", data=payload,
                             headers={"content-type": "application/json"})
with urllib.request.urlopen(req) as r:
    print("python", json.loads(r.read())["choices"][0]["message"]["content"])
'''

SDK_CLIENT = '''\
import openai

client = openai.OpenAI()
reply = client.chat.completions.create(model="gpt-4o-mini",
                                       messages=[{"role": "user", "content": "hi"}])
print("sdk", reply.choices[0].message.content)
'''

SDK_NODE_CLIENT = '''\
import openai
from blackbox import api

client = openai.OpenAI()
with api.run("answer"):
    reply = client.chat.completions.create(model="gpt-4o-mini",
                                           messages=[{"role": "user", "content": "hi"}])
print("sdk-nodes", reply.choices[0].message.content)
'''

NODE_CLIENT = '''\
const http = require('http');
const base = new URL(process.env.OPENAI_BASE_URL);
const body = JSON.stringify({model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}]});
const req = http.request({host: base.hostname, port: base.port,
  path: base.pathname.replace(/\\/$/, '') + '/chat/completions', method: 'POST',
  headers: {'content-type': 'application/json', 'content-length': Buffer.byteLength(body)}}, res => {
  let data = ''; res.on('data', c => data += c);
  res.on('end', () => {
    if (res.statusCode !== 200) { console.error('status', res.statusCode, data); process.exit(1); }
    console.log('node', JSON.parse(data).choices[0].message.content);
  });
});
req.on('error', e => { console.error('node error', e.message); process.exit(1); });
req.end(body);
'''


class Upstream(BaseHTTPRequestHandler):
    """Enough of a chat-completions endpoint to be a real HTTP origin on a real socket."""

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "id": "chatcmpl-accept", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                            # keep the report readable
        pass


def start_upstream() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def frame_kinds(raw: bytes) -> dict[str, int]:
    """Count the frame kinds in one segment, by name, without trusting the manifest's summary of them."""
    counts: dict[str, int] = {}
    off = SEGMENT_HEADER_BYTES
    while off < len(raw):
        try:
            frame, off = frame_mod.decode(raw, off)
        except Exception:
            break
        name = KIND_NAMES.get(frame["kind"], str(frame["kind"]))
        counts[name] = counts.get(name, 0) + 1
    return counts


def _has_openai() -> bool:
    """An SDK case only runs where the SDK is installed; if it is, the artefact says which version produced it."""
    try:
        import openai  # noqa: F401
    except Exception:
        return False
    return True


def _openai_version() -> str | None:
    try:
        import openai

        return getattr(openai, "__version__", None)
    except Exception:
        return None


def inspect(data_dir: Path) -> list[dict]:
    """Every capsule the run left, read the way a user's diagnosis would read it."""
    out = []
    for path in sorted((data_dir / "capsules").glob("*.bbx")):
        capsule = cap.read_capsule(str(path))
        manifest = capsule.manifest
        kinds = frame_kinds(capsule.frames_of(1))
        try:
            links = json.loads(capsule.entry("links.json"))
        except Exception as exc:                             # a missing or broken links.json is a finding, not a crash
            links = {"error": f"{type(exc).__name__}: {exc}"}
        out.append({
            "capsule": path.name,
            "root_ok": capsule.root_ok,
            "run_id": (manifest.get("run") or {}).get("run_id"),
            "run_status": (manifest.get("run") or {}).get("status"),
            "counts": manifest.get("counts"),
            "frames": kinds,
            "has_proxy_vantage": all(k in kinds for k in (KIND_NAMES[KIND_MODEL_REQUEST],
                                                          KIND_NAMES[KIND_MODEL_CHUNK],
                                                          KIND_NAMES[KIND_MODEL_RESPONSE])),
            "nodes": (manifest.get("counts") or {}).get("nodes"),
            "links": links.get("summary") or links,
            "link_pairs": [{"class": link.get("class"), "left_run": link.get("left_run"),
                            "right_run": link.get("right_run")} for link in (links.get("links") or [])],
        })
    return out


def graphs(data_dir: Path) -> list[dict]:
    """What each tape's own `graph.json` says, because `counts.nodes` in a manifest is a summary of this file.

    The ticket's sentence is about *nodes*, and a run can pass every other check while its graph lists none, which is
    exactly how it looked before defect 49 was closed. Reading the graph rather than the count keeps the report about
    the artefact a user opens.
    """
    out = []
    for path in sorted((data_dir / "runs").glob("*/graph.json")):
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                             # a graph that cannot be read is a finding, not a crash
            out.append({"run_dir": path.parent.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        nodes = graph.get("nodes") or []
        out.append({"run_dir": path.parent.name, "writer_uuid": graph.get("writer_uuid"),
                    "run_id": graph.get("run_id"), "graph_nodes": len(nodes), "graph_edges": len(graph.get("edges") or []),
                    "open_nodes": graph.get("open_nodes"),
                    "node_statuses": sorted({str(n.get("status")) for n in nodes}),
                    "node_slots": sorted({n.get("slot") for n in nodes})})
    return out


CLIENTS = {"python": PY_CLIENT, "node": NODE_CLIENT, "sdk": SDK_CLIENT, "sdk-nodes": SDK_NODE_CLIENT}


def run_client(work: Path, data: Path, upstream: str, name: str, cmd: list[str], client: Path) -> dict:
    client.write_text(CLIENTS[name], encoding="utf-8")
    argv = [sys.executable, "-m", "blackbox", "run", "--data-dir", str(data),
            "--upstream", f"openai={upstream}@/openai", "--", *cmd]
    # The SDK needs *a* key to build a client at all; the origin never checks it, and this is a loopback run.
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), OPENAI_API_KEY=os.environ.get("OPENAI_API_KEY", "sk-accept-local"))
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=str(work), env=env, capture_output=True, text=True, timeout=180)
    capsules = inspect(data)
    run_ids = {c["run_id"] for c in capsules}
    graph = graphs(data)
    return {
        "client": name,
        "command": cmd,
        "exit_code": proc.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "stdout": proc.stdout.strip().splitlines()[-3:],
        "stderr_tail": proc.stderr.strip().splitlines()[-3:],
        "capsules": capsules,
        "capsule_count": len(capsules),
        "every_root_ok": bool(capsules) and all(c["root_ok"] for c in capsules),
        "proxy_vantage_recorded": any(c["has_proxy_vantage"] for c in capsules),
        "one_run_id_across_capsules": len(run_ids) == 1,
        "run_ids_seen": sorted(r for r in run_ids if r),
        "links": [pair for c in capsules for pair in c["link_pairs"]],
        "graphs": graph,
        "total_nodes": sum(g["graph_nodes"] for g in graph if "graph_nodes" in g),
        "exact_pairs": [pair for c in capsules for pair in c["link_pairs"] if pair.get("class") == "exact"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P3B-03 acceptance: node and python children, one run each")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-16-p3b03-acceptance.json"))
    args = ap.parse_args(argv)

    node = shutil.which("node")
    upstream, upstream_url = start_upstream()
    work = Path(tempfile.mkdtemp(prefix="bbx-accept-"))
    results = []
    try:
        cases = [("python", [sys.executable, "client.py"], work / "client.py")]
        if node:
            cases.append(("node", [node, "client.js"], work / "client.js"))
        if _has_openai():
            cases.append(("sdk", [sys.executable, "sdk_client.py"], work / "sdk_client.py"))
            # The ticket's sentence is about *nodes*: an app that labels its work with `api.run` is the only kind
            # that can produce one, so the harness has to run one or the report cannot answer the question.
            cases.append(("sdk-nodes", [sys.executable, "sdk_node_client.py"], work / "sdk_node_client.py"))
        for name, cmd, client in cases:
            data = work / f"data-{name}"
            results.append(run_client(work, data, upstream_url, name, cmd, client))
    finally:
        upstream.shutdown()

    node_version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip() if node else None
    report = {
        "ticket": "P3B-03",
        "question": "does `blackbox run -- node app.js` / `-- python app.py` yield one run with linked nodes?",
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {"platform": platform.platform(), "python": platform.python_version(), "node": node_version,
                 "openai": _openai_version()},
        "upstream": upstream_url,
        "results": results,
        "verdict": {
            "proxy_vantage_recorded": all(r["proxy_vantage_recorded"] for r in results),
            "every_capsule_root_ok": all(r["every_root_ok"] for r in results),
            "both_clients_exit_zero": all(r["exit_code"] == 0 for r in results),
            "one_run_id_across_capsules": all(r["one_run_id_across_capsules"] for r in results),
            # A clean run's tape is sealed by its writer's own close, so the capsule must not call the run interrupted
            # (defect 53: every clean run said SEAL_INTERRUPTED because the scanner never read the active segment's seal).
            "clean_runs_are_not_reported_as_interrupted": all(
                c["run_status"] == "recovered" for r in results for c in r["capsules"]),
            # Only a run with two capsules can show a link; a single-capsule run has no sibling to be linked to, and
            # asserting otherwise would fail a case for being captured by one vantage rather than two.
            "exact_links_join_every_run_with_two_capsules": all(
                r["exact_pairs"] for r in results if r["capsule_count"] > 1),
            # Nodes come from the app's own labels (`api.run`), so the instrumented case is the one that can answer
            # this; an unmodified client records a model call and no node, which is the product's design, not a miss.
            "app_nodes_are_in_the_graph": {r["client"]: r["total_nodes"] for r in results},
            # The half that is still open, reported rather than omitted: `graph.json`'s `edges` is always empty, so the
            # cross-vantage relation lives in `links.json` and not in the graph the ticket's sentence names (defect 55).
            "graph_has_cross_vantage_edges": all(
                g.get("graph_edges", 0) for r in results for g in r["graphs"] if g.get("graph_nodes")),
            "node_available": node is not None,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for result in results:
        print(f"{result['client']:6} exit {result['exit_code']} capsules {result['capsule_count']} "
              f"proxy-vantage {result['proxy_vantage_recorded']} roots {result['every_root_ok']} "
              f"one-run-id {result['one_run_id_across_capsules']} ({', '.join(result['run_ids_seen'])}) "
              f"nodes {result['total_nodes']} exact {len(result['exact_pairs'])}")
    print("verdict:", json.dumps(report["verdict"], sort_keys=True))
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
