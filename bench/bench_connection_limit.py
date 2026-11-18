"""B3: what happens at the proxy's connection ceiling, measured rather than asserted.

The blocker's claim was that the ceiling was the *descriptor table* and that nothing decided what happened at it: a probe
issued during a 450-request burst timed out after 4 s, and asyncio logged a traceback per `accept`. `proxy/limits.py`
raised the soft limit (the part that is not a decision). This bench measures the part that is: `MAX_INFLIGHT = 200`
requests in flight with backpressure above it, `MAX_CONNECTIONS = 512` held clients, a bounded queue wait, and an
explicit `503` with `x-blackbox-error` where the recorder genuinely cannot serve.

Three measurements, each of them a thing a child can observe:

1. **450 concurrent requests** (the burst that timed out before) — every one is answered, two are in flight at most
   `MAX_INFLIGHT` at a time, and the ones above the cap wait instead of failing. The claim is "0 refused".
2. **a probe during that burst** — served, and how long it took. This is the row's original failure.
3. **more clients than `MAX_CONNECTIONS`** — the surplus gets a `503` naming the ceiling, in milliseconds, and never a
   connection that hangs. The claim is "answered, and answered fast".

The origin is a real asyncio server that sleeps a fixed time before answering, so "in flight" is a fact about requests
parked in it, not a guess. Every number is written to `bench/results/2026-11-16-b3-connection-limit.json`.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.limits import raise_fd_limit
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ERROR_HEADER, MAX_CONNECTIONS, MAX_INFLIGHT, WORST_CASE_FDS, ProxyServer

BURST = 450
SURPLUS = 96                       # clients opened beyond `MAX_CONNECTIONS`
ORIGIN_DELAY_S = 0.30
OUT = ROOT / "bench" / "results" / "2026-11-16-b3-connection-limit.json"


class Origin:
    def __init__(self) -> None:
        self.port = 0
        self.active = 0
        self.peak = 0
        self.served = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> Origin:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int((self._server.sockets or ())[0].getsockname()[1])
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(ORIGIN_DELAY_S)
                finally:
                    self.active -= 1
                self.served += 1
                body = b'{"ok":true}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: keep-alive\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


def _call(port: int, timeout: float = 60.0) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(b"GET /v1/models HTTP/1.1\r\nHost: recorder\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
        raw = b"".join(chunks)
        status = int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/1.") else 0
        reason = ""
        for line in raw.split(b"\r\n"):
            if line.lower().startswith(ERROR_HEADER.encode() + b":"):
                reason = line.split(b":", 1)[1].strip().decode()
        return status, reason, (time.monotonic() - started) * 1000.0
    except OSError as exc:
        return 0, f"{type(exc).__name__}: {exc}", (time.monotonic() - started) * 1000.0


def main() -> int:
    # The bench holds every client socket *and* hosts the proxy, so it needs the same descriptor budget a launcher would
    # have: `raise_fd_limit()` is what production does before the listener starts. It is recorded, not assumed.
    fd_limit = raise_fd_limit()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    origin = loop.run_until_complete(Origin().start())
    table = Routes([Route(name="openai", base=f"http://127.0.0.1:{origin.port}")], default="openai")
    proxy = loop.run_until_complete(ProxyServer(table).start())
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def stats() -> dict:
        async def read() -> dict:
            return proxy.stats.as_dict()
        return asyncio.run_coroutine_threadsafe(read(), loop).result(10)

    out: dict = {
        "what": "B3 - behaviour at the proxy's connection ceiling",
        "caps": {"max_inflight": MAX_INFLIGHT, "max_connections": MAX_CONNECTIONS,
                 "worst_case_fds": WORST_CASE_FDS},
        "hardware": {"python": platform.python_version(), "platform": platform.platform(),
                     "cpus": os.cpu_count()},
        "origin_delay_s": ORIGIN_DELAY_S,
        "fd_limit": {"before": fd_limit[0] if fd_limit else None, "after": fd_limit[1] if fd_limit else None},
    }
    try:
        # 1+2: 450 concurrent requests, and a probe issued while they are in flight.
        answers: list[tuple[int, str, float]] = []
        lock = threading.Lock()

        def burst_one() -> None:
            answer = _call(proxy.port)
            with lock:
                answers.append(answer)

        peak_samples: list[int] = []

        def sample_peak(live: list[threading.Thread]) -> None:
            while any(thread.is_alive() for thread in live):
                peak_samples.append(origin.peak)
                time.sleep(0.02)
            peak_samples.append(origin.peak)

        threads = [threading.Thread(target=burst_one, daemon=True) for _ in range(BURST)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        sampler = threading.Thread(target=sample_peak, args=(threads,), daemon=True)
        sampler.start()
        time.sleep(0.15)
        probe = _call(proxy.port)
        elapsed = time.monotonic() - started
        for thread in threads:
            thread.join(60)
        sampler.join(10)
        statuses = [answer[0] for answer in answers]
        out["burst"] = {
            "requests": BURST,
            "statuses": {str(code): statuses.count(code) for code in sorted(set(statuses))},
            "wall_ms": round(elapsed * 1000.0, 1),
            "median_ms": round(statistics.median(answer[2] for answer in answers), 1),
            "max_ms": round(max(answer[2] for answer in answers), 1),
            "origin_peak_in_flight": max(peak_samples or [0]),
            "served_by_origin": origin.served,
        }
        out["probe_during_burst"] = {"status": probe[0], "ms": round(probe[2], 1), "reason": probe[1]}
        out["stats_after_burst"] = stats()

        # 3: more connections than MAX_CONNECTIONS. The surplus must be answered, not left to hang.
        time.sleep(0.5)                                    # let the burst's connections go before the ceiling is filled
        holders = []
        for _ in range(MAX_CONNECTIONS):
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            sock.sendall(b"GET /v1/models HTTP/1.1\r\nHost: recorder\r\n\r\n")   # parked in the slow origin
            holders.append(sock)
        time.sleep(0.2)
        surplus: list[tuple[int, str, float]] = []
        surplus_threads = [threading.Thread(target=lambda: surplus.append(_call(proxy.port, timeout=30)), daemon=True)
                           for _ in range(SURPLUS)]
        for thread in surplus_threads:
            thread.start()
        for thread in surplus_threads:
            thread.join(60)
        codes = [answer[0] for answer in surplus]
        out["surplus"] = {
            "clients_over_cap": SURPLUS,
            "statuses": {str(code): codes.count(code) for code in sorted(set(codes))},
            "median_ms": round(statistics.median(answer[2] for answer in surplus), 1) if surplus else None,
            "max_ms": round(max((answer[2] for answer in surplus), default=0.0), 1),
            "reasons": sorted({answer[1] for answer in surplus}),
        }
        out["stats_after_surplus"] = stats()
        for sock in holders:
            sock.close()
    finally:
        async def shutdown() -> None:
            await proxy.close()
            await origin.close()
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(shutdown(), loop).result(15)
        loop.call_soon_threadsafe(loop.stop)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
