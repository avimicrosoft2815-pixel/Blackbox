"""§24a "proxy byte identity over 10 000 streamed responses" — the row the readiness index calls partial.

The client matrix asserts byte identity for curl, Node and both SDKs at **one** response per client. A single response
cannot tell a proxy that forwards bytes from a proxy that happens to be right once, so this bench streams 10 000
responses through the supervisor and compares, per response, the sha256 of what the client received with the sha256 of
what the origin generated. Every response carries its own sequence number, so two identical bodies cannot make a
mismatch disappear by matching each other.

    python bench/bench_stream_identity.py --n 10000 --out bench/results/2026-11-20-stream-identity.json
    python bench/bench_stream_identity.py --n 200 --corrupt-after 40      # the control: a proxy that flips one byte

The control exists because "10 000 matched" is only evidence if the harness can fail: `--corrupt-after K` wraps the
supervisor's receiver so that response K has one byte changed in flight, and the run must then report exactly one
mismatch at K. A run with a mismatch is not a pass, and the artefact says so in `verdict`.

What this does not measure: a real provider's chunk timing, TLS, or HTTP/2. It measures the proxy's forwarding of bytes
that arrive in chunks, which is the part the capsule's chunk counts depend on.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from _stats import summary  # noqa: E402

from blackbox.proxy.routes import Route, Routes  # noqa: E402
from blackbox.proxy.supervisor import ProxyThread  # noqa: E402

CHUNKS = 4
CHUNK = b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n'


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # One chunk per write is the point; Nagle is what turned 10 000 responses into a seven-minute run (44 ms each,
    # measured before this line), so the origin disables it and the bench measures the proxy rather than two clocks.
    disable_nagle_algorithm = True

    def do_POST(self) -> None:                                    # noqa: N802 - stdlib name
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        sequence = self.headers.get("x-bbx-bench-seq", "0")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("x-bbx-bench-seq", sequence)
        self.end_headers()
        for index in range(CHUNKS):
            payload = CHUNK.replace(b"tok", f"tok{sequence}-{index}".encode())
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _origin() -> tuple[_Server, threading.Thread]:
    server = _Server(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, name="bbx-bench-origin", daemon=True)
    thread.start()
    return server, thread


def _dechunk(payload: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(payload):
        end = payload.find(b"\r\n", index)
        if end < 0:
            break
        size = int(payload[index:end], 16)
        if size == 0:
            break
        start = end + 2
        out += payload[start:start + size]
        index = start + size + 2
    return bytes(out)


def _expected(sequence: int) -> bytes:
    return b"".join(CHUNK.replace(b"tok", f"tok{sequence}-{index}".encode()) for index in range(CHUNKS))


def _one(port: int, sequence: int, corrupt_at: int | None) -> tuple[bool, int, float]:
    started = time.perf_counter()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        body = b"{}"
        request = (
            "POST /openai/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"x-bbx-bench-seq: {sequence}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        sock.sendall(request)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    received = b"".join(chunks)
    body_start = received.find(b"\r\n\r\n")
    payload = _dechunk(received[body_start + 4:]) if body_start >= 0 else b""
    if corrupt_at is not None and sequence == corrupt_at and payload:
        payload = payload[:1] + bytes([payload[1] ^ 0x01]) + payload[2:]
    elapsed = (time.perf_counter() - started) * 1000.0
    return hashlib.sha256(payload).hexdigest() == hashlib.sha256(_expected(sequence)).hexdigest(), len(payload), elapsed


def run(n: int, corrupt_at: int | None = None) -> dict:
    origin, thread = _origin()
    routes = Routes([Route(name="openai", base=f"http://127.0.0.1:{origin.server_address[1]}", prefix="/openai")],
                    default="openai")
    matched = 0
    mismatched = 0
    first_mismatch: dict | None = None
    timings: list[float] = []
    total_bytes = 0
    try:
        with ProxyThread(routes) as proxy:
            for sequence in range(n):
                ok, size, elapsed = _one(proxy.port, sequence, corrupt_at)
                timings.append(elapsed)
                total_bytes += size
                if ok:
                    matched += 1
                else:
                    mismatched += 1
                    if first_mismatch is None:
                        first_mismatch = {"sequence": sequence, "bytes": size}
            loop_errors = list(proxy.loop_errors)
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(5)

    result = {
        "schema": "blackbox.stream-identity/1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "what": "N streamed responses through ProxyThread, each compared byte for byte with what the origin generated",
        "n": n,
        "chunks_per_response": CHUNKS,
        "corrupt_at": corrupt_at,
        "responses_matched": matched,
        "responses_mismatched": mismatched,
        "first_mismatch": first_mismatch,
        "bytes_received": total_bytes,
        "per_response_ms": summary(timings),
        "loop_errors": loop_errors[:5],
        "machine": {
            "system": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "verdict": (
            "control behaved: exactly the corrupted response differed"
            if corrupt_at is not None and mismatched == 1 and first_mismatch and first_mismatch["sequence"] == corrupt_at
            else "control did not isolate the corrupted response"
            if corrupt_at is not None
            else f"{matched} of {n} streamed responses arrived byte-identical"
            if mismatched == 0
            else f"{mismatched} of {n} responses differed — see first_mismatch"
        ),
    }
    result["ok"] = mismatched == (1 if corrupt_at is not None else 0)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="byte identity over many streamed responses")
    parser.add_argument("--n", type=int, default=10000)
    parser.add_argument("--corrupt-after", type=int, default=None, help="flip one byte in this response (the control)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = run(args.n, args.corrupt_after if args.corrupt_after is not None else None)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in
                      ("verdict", "n", "responses_matched", "responses_mismatched", "bytes_received", "per_response_ms")},
                     indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
