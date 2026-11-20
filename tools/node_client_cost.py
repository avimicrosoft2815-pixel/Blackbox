#!/usr/bin/env python3
"""What a Node client actually costs through the proxy — evidence for defect 113, not a claim about Windows.

Defect 113: one `py3.12 / windows-latest` cell timed out twice on a 60-second `subprocess.run` of a seven-line Node
script, on a commit whose `py3.11` and `py3.13` Windows cells passed and whose identical `py3.12` job passed on a second
run 25 s later. The test's budget is 60 s. The question is whether 60 s is a *tight* budget for a loopback request
through the proxy, or roughly two orders of magnitude past the work.

This tool answers with the distribution rather than an opinion: it starts the proxy on a mock-provider origin, then runs
the same shape of Node client N times and records the wall time of each child process — interpreter start, script parse,
connect, body, exit — and the same through `curl` for a second runtime. What it cannot do is run on a Windows runner, so
the numbers are this host's floor and the report says so.

    PYTHONPATH=src python tools/node_client_cost.py --n 25 --out bench/results/<date>-node-client-cost.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.cap_sdk.mock_provider import MockProvider, Scenario   # noqa: E402
from blackbox.proxy.routes import Route, Routes                     # noqa: E402
from blackbox.proxy.supervisor import ProxyThread                   # noqa: E402

NODE = shutil.which("node")
CURL = shutil.which("curl")

SCRIPT = (
    "const http = require('http');\n"
    "http.get(process.argv[2], (res) => {\n"
    "  const chunks = [];\n"
    "  res.on('data', (c) => chunks.push(c));\n"
    "  res.on('end', () => { process.stdout.write(Buffer.concat(chunks).toString('base64')); });\n"
    "}).on('error', (e) => { process.stderr.write(String(e)); process.exit(1); });\n"
)


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[max(0, int(round(0.95 * (len(ordered) - 1))))], 1),
        "max_ms": round(ordered[-1], 1),
    }


def _run_node(script: Path, url: str) -> float:
    started = time.perf_counter()
    subprocess.run([NODE, str(script), url], capture_output=True, check=True, timeout=120)
    return (time.perf_counter() - started) * 1000.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=25)
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    record: dict[str, Any] = {
        "schema": "blackbox.node-client-cost/1",
        "what_this_measures": (
            "wall time of a whole client process per request through the proxy, on this host. It measures the *test's* "
            "budget against the work, not the product's latency: the proxy's own per-response time is measured in "
            "bench/results/*-stream-identity.json."),
        "machine": {
            "system": os.uname().sysname if hasattr(os, "uname") else sys.platform,
            "python": sys.version.split()[0],
            "node": subprocess.run([NODE, "--version"], capture_output=True, text=True, check=True).stdout.strip()
            if NODE else None,
            "curl": subprocess.run([CURL, "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
            if CURL else None,
            "cpus": os.cpu_count(),
        },
        "test_budget_ms": 60_000,
    }

    scenario = Scenario(chunks=4, delay_ms=0, chunk_text="tok ")
    with MockProvider(scenario) as provider, tempfile.TemporaryDirectory(prefix="bbx-node-cost-") as scratch:
        routes = Routes([Route(name="openai", base=provider.openai_base_url(), prefix="/openai")], default="openai")
        proxy = ProxyThread(routes)
        proxy.start()
        try:
            url = f"http://127.0.0.1:{proxy.port}/openai/v1/models"
            if NODE:
                script = Path(scratch) / "client.js"
                script.write_text(SCRIPT, encoding="utf-8")
                _run_node(script, url)                       # warm-up: the first run pays for the OS's page cache
                node_ms = [_run_node(script, url) for _ in range(args.n)]
                record["node"] = {**_summary(node_ms), "sampled_ms": [round(v, 1) for v in node_ms]}
            else:
                record["node"] = {"skipped": "node is not installed on this host"}
            if CURL:
                _curl(url)
                curl_ms = [_curl(url) for _ in range(args.n)]
                record["curl"] = _summary(curl_ms)
            else:
                record["curl"] = {"skipped": "curl is not installed on this host"}
            record["proxy_stats"] = {"requests": proxy.stats.requests}
        finally:
            proxy.stop()

    node = record.get("node", {})
    if "max_ms" in node:
        record["budget_relative_to_this_host"] = {
            "max_over_budget": round(60_000 / max(node["max_ms"], 0.001), 1),
            "note": ("how many times the slowest whole Node process here fits inside the test's 60-second budget. A "
                     "runner several times slower than this host moves the figure, not the order of magnitude; a runner "
                     "that hangs a connect does not, which is why the defect is left open rather than called a flake."),
        }
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(text)
    return 0


def _curl(url: str) -> float:
    started = time.perf_counter()
    subprocess.run([CURL, "-sS", "--http1.1", "-N", "--max-time", "30", url], capture_output=True, check=True)
    return (time.perf_counter() - started) * 1000.0


if __name__ == "__main__":
    raise SystemExit(main())
