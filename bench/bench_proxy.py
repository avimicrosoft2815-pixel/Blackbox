"""P3B-08: the four §15.1 proxy rows, measured against a real listener and a real origin on loopback.

The budgets (§15.1):

| row | budget | how it is measured here |
|---|---|---|
| added latency, non-stream, loopback | p50 ≤ 1 ms, p99 ≤ 5 ms | 500 small POSTs through the proxy, minus the same 500 sent straight at the origin, paired per request |
| first-byte passthrough overhead (SSE) | ≤ 0.5 ms | time to the first SSE byte through the proxy minus the same at the origin, paired |
| concurrent streams | ≥ 200 without errors | 200 streams opened at once, each reading its whole body, all sha256-checked |
| RSS at 50 req/s | ≤ 60 MB | the recording process's RSS growth while 50 req/s runs for 10 s |

Why *paired differences* rather than two averages: the origin and the proxy share a machine and a loopback stack, so
the interesting quantity is the difference between two requests that carried the same bytes, not the difference between
two distributions measured minutes apart. The bench also reports the raw through-the-proxy numbers, because a reader
who wants "how fast is a proxied call" should not have to add two columns together.

Nothing here is a gate unless it is labelled `gate: true` in the JSON, and every row carries `n`.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import multiprocessing
import os
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from _stats import summary

JSON_BODY = b'{"choices":[{"message":{"content":"ok"}}],"id":"chatcmpl-bench"}'
SSE_CHUNKS = [b'data: {"choices":[{"delta":{"content":"tok%d"}}]}\n\n' % index for index in range(14)]
SSE_BODY = b"".join(SSE_CHUNKS)
CONCURRENCY = 200
RATE_HZ = 50.0
RATE_SECONDS = 10.0


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # A real origin (uvicorn, gunicorn, any Go server) sets `TCP_NODELAY`. Leaving it off here would measure a 40 ms
    # Nagle/delayed-ACK stall in the *fixture* and call it proxy latency, which is a bench lying about its subject.
    disable_nagle_algorithm = True

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        if "/sse" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in SSE_CHUNKS:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(JSON_BODY)))
            self.end_headers()
            self.wfile.write(JSON_BODY)

    def log_message(self, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    request_queue_size = 512               # 200 concurrent streams must not be refused by the fixture's backlog
    daemon_threads = True


def _origin_process(ready: Any) -> None:
    """The origin, in its own process, because the GIL is part of what a proxy costs.

    The first version of this bench kept the origin, the proxy and the measuring client inside one interpreter. Every
    hop then paid a GIL hand-off, and the delta it reported was a property of CPython's threading rather than of the
    proxy: the first-byte row landed at 0.44–0.52 ms against a 0.5 ms budget, run to run. A proxy talks to a provider in
    another process; the harness now does too, and the proxy thread is the only other thread in this one.
    """
    server = _Server(("127.0.0.1", 0), _Origin)
    ready.send(server.server_address[1])
    server.serve_forever()


def _origin_port() -> tuple[int, Any]:
    """`(port, process)` — the caller terminates the process. `fork` where it exists, `spawn` where it does not."""
    context = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_origin_process, args=(child,), daemon=True)
    process.start()
    port = parent.recv()
    return port, process


def _proxy_process(conn: Any, route_bases: dict[str, str]) -> None:
    """The proxy in its own process — which is where it runs in production, and what makes these rows mean anything.

    A proxy that shares an interpreter with the client it serves pays a GIL hand-off on every hop, and this bench spent
    two revisions measuring that instead of the proxy (first byte 0.44–0.52 ms, run to run, against a 0.5 ms budget).
    The hop from a client process to a proxy process is a socket; here it is one too.
    """
    from blackbox.proxy.routes import Route, Routes
    from blackbox.proxy.server import ProxyServer

    async def serve() -> None:
        routes = Routes([Route(name=name, base=base, prefix=f"/{name}")
                         for name, base in route_bases.items()], default="origin")
        server = await ProxyServer(routes).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        conn.send({"port": server.port, "pid": os.getpid()})

        def reader() -> None:
            while True:
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    return
                if message == "stats":
                    conn.send({"stats": server.stats.as_dict()})
                elif message == "stop":
                    loop.call_soon_threadsafe(stop.set)
                    return

        threading.Thread(target=reader, daemon=True).start()
        await stop.wait()
        await server.close()

    asyncio.run(serve())


def _start_proxy_process(route_bases: dict[str, str]) -> tuple[int, int, Any, Any]:
    """`(port, pid, connection, process)`."""
    context = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_proxy_process, args=(child, route_bases), daemon=True)
    process.start()
    hello = parent.recv()
    return hello["port"], hello["pid"], parent, process


def _rss_kib(pid: int | None = None) -> int:
    path = f"/proc/{pid}/status" if pid else "/proc/self/status"
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _request(port: int, path: str, *, stream: bool = False) -> tuple[float, float, bytes]:
    """One request over a fresh socket: returns (first-byte ms, total ms, body)."""
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    started = time.monotonic()
    first = None
    try:
        connection.request("POST", path, body=b'{"model":"bench","stream":true}',
                           headers={"content-type": "application/json"})
        response = connection.getresponse()
        out = bytearray()
        while True:
            piece = response.read(256 if stream else 65536)
            if not piece:
                break
            if first is None:
                first = (time.monotonic() - started) * 1000.0
            out += piece
        return (first if first is not None else (time.monotonic() - started) * 1000.0,
                (time.monotonic() - started) * 1000.0, bytes(out))
    finally:
        connection.close()


async def _streams_rss(proxy_port: int, origin_port: int, proxy_pid: int) -> dict:
    """200 concurrent streams, then 50 req/s for 10 s, with RSS sampled from this process."""
    async def one(index: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        # `Connection: close` so the response ends at EOF: 200 concurrent chunk parsers would be measuring the bench's
        # parser rather than the proxy, and this way each body is exactly what the origin pushed.
        writer.write(b"POST /sse HTTP/1.1\r\nHost: bench\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        data = await reader.read()
        writer.close()
        with_context = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else data
        return _dechunk(with_context)

    started = time.monotonic()
    bodies = await asyncio.gather(*[one(index) for index in range(CONCURRENCY)])
    elapsed = (time.monotonic() - started) * 1000.0
    digests = {hashlib.sha256(body).hexdigest() for body in bodies}
    ok = digests == {hashlib.sha256(SSE_BODY).hexdigest()}

    baseline = _rss_kib(proxy_pid)
    peak = baseline
    deadline = time.monotonic() + RATE_SECONDS
    sent = 0
    while time.monotonic() < deadline:
        batch_started = time.monotonic()
        await asyncio.gather(*[one(sent + offset) for offset in range(int(RATE_HZ / 20))])
        sent += int(RATE_HZ / 20)
        peak = max(peak, _rss_kib(proxy_pid))
        sleep = 0.05 - (time.monotonic() - batch_started)
        if sleep > 0:
            await asyncio.sleep(sleep)
    return {
        "concurrent_streams": {"count": CONCURRENCY, "wall_ms": round(elapsed, 3), "bodies_identical": ok,
                               "errors": 0 if ok else 1},
        "rss": {"requests": sent, "rate_hz": RATE_HZ, "seconds": RATE_SECONDS,
                "baseline_kib": baseline, "peak_kib": peak, "growth_mib": round((peak - baseline) / 1024.0, 3)},
    }


def _proxy_stats(conn: Any) -> dict:
    """Ask the proxy process for its counters. The pipe is duplex; the child answers one message at a time."""
    try:
        conn.send("stats")
        reply = conn.recv()
        return dict(reply.get("stats") or {})
    except (EOFError, OSError):
        return {}


def _dechunk(payload: bytes) -> bytes:
    out = bytearray()
    rest = payload
    while rest:
        line, _, tail = rest.partition(b"\r\n")
        try:
            size = int(line.strip() or b"0", 16)
        except ValueError:
            return bytes(payload)
        if size == 0:
            break
        out += tail[:size]
        rest = tail[size + 2:]
    return bytes(out)


def run(n: int, streams: bool) -> dict:
    """Origin and proxy each in their own process; the measuring client is this one, and it is synchronous.

    Both halves of that are load-bearing. A client that blocks the same event loop the proxy needs cannot measure the
    proxy, it measures a deadlock; a proxy that *shares an interpreter* with the client pays a GIL hand-off on every hop,
    and the first two revisions of this bench measured exactly that (first byte 0.44–0.52 ms, PASS/FAIL alternating,
    against a 0.5 ms budget) rather than the proxy. The hop from the client process to the proxy process is a socket;
    here, now, it is one too.
    """
    origin_port, origin_process = _origin_port()
    proxy_port, proxy_pid, conn, proxy_process = _start_proxy_process({"origin": f"http://127.0.0.1:{origin_port}"})
    try:
        for _ in range(20):                                      # warm-up, discarded
            _request(origin_port, "/plain")
            _request(proxy_port, "/origin/plain")
        direct_latency: list[float] = []
        proxied_latency: list[float] = []
        added: list[float] = []
        for _ in range(n):
            d_first, d_total, _body = _request(origin_port, "/plain")
            p_first, p_total, _body2 = _request(proxy_port, "/origin/plain")
            direct_latency.append(d_total)
            proxied_latency.append(p_total)
            added.append(p_total - d_total)

        first_direct: list[float] = []
        first_proxied: list[float] = []
        first_added: list[float] = []
        for _ in range(max(20, n // 5)):
            d_first, _d_total, _ = _request(origin_port, "/sse", stream=True)
            p_first, _p_total, _ = _request(proxy_port, "/origin/sse", stream=True)
            first_direct.append(d_first)
            first_proxied.append(p_first)
            first_added.append(p_first - d_first)

        report = {
            "added_latency_ms": {"direct": summary(direct_latency), "proxied": summary(proxied_latency),
                                 "added": summary(added)},
            "first_byte_ms": {"direct": summary(first_direct), "proxied": summary(first_proxied),
                              "added": summary(first_added),
                              # The block medians are the honesty check: one p50 from one long run hides a row that
                              # is close to its budget, and this row is the closest in the file.
                              "block_p50": [summary(first_added[i:i + 20])["p50"]
                                            for i in range(0, len(first_added), 20)]},
            "gate": {
                "added_latency_ms": {"n": summary(added)["n"], "p50": summary(added)["p50"],
                                     "p99": summary(added)["p99"], "budget_p50": 1.0, "budget_p99": 5.0,
                                     "pass": summary(added)["p50"] <= 1.0 and summary(added)["p99"] <= 5.0},
                "first_byte_ms": {"n": summary(first_added)["n"], "p50": summary(first_added)["p50"],
                                  "budget": 0.5, "pass": summary(first_added)["p50"] <= 0.5},
            },
            "stats": _proxy_stats(conn),
        }
        if streams:
            report.update(asyncio.run(_streams_rss(proxy_port, origin_port, proxy_pid)))
            report["gate"]["concurrent_streams"] = {
                "n": report["concurrent_streams"]["count"], "count": report["concurrent_streams"]["count"],
                "pass": report["concurrent_streams"]["bodies_identical"],
                "budget": 200,
            }
            report["gate"]["rss"] = {"n": report["rss"]["requests"], "growth_mib": report["rss"]["growth_mib"],
                                     "budget_mib": 60.0,
                                     "pass": report["rss"]["growth_mib"] <= 60.0}
        report["proxy_pid"] = proxy_pid
        report["origin_pid"] = origin_process.pid
        return report
    finally:
        with contextlib.suppress(Exception):
            conn.send("stop")
        proxy_process.terminate()
        proxy_process.join(10)
        origin_process.terminate()
        origin_process.join(10)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--no-streams", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / f"{time.strftime('%Y-%m-%d')}-proxy.json"))
    args = ap.parse_args(argv)
    report = run(args.n, not args.no_streams)
    report["hardware"] = {"cpu_count": os.cpu_count(), "python": platform.python_version(),
                          "implementation": platform.python_implementation(), "os": platform.platform(),
                          "machine": platform.machine(), "n": args.n, "pid": os.getpid(),
                          "topology": "three processes: origin, proxy and the measuring client each in their own "
                                       "interpreter, because a shared one makes every row here a measurement of the "
                                       "GIL hand-off rather than of the proxy"}
    report["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row, verdict in report["gate"].items():
        state = "PASS" if verdict.get("pass") else "FAIL"
        print(f"{row:22s} {state}  {json.dumps({k: v for k, v in verdict.items() if k != 'pass'})}")
    print(f"wrote {args.out}")
    return 0 if all(v.get("pass") for v in report["gate"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
