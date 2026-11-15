"""S16, the proxy vantage: kill the proxy mid-stream and read the tape it left behind.

`INTERRUPTED_PROXY` is the one class of §14.4a that nobody can write. The process that would have written the terminal
frame is the process that is gone, so the evidence is an *absence* — an open call on the tape, and a writer whose lease
is dead — and the only honest way to test it is to kill a real proxy in the middle of a real response and then read the
tape from outside, as a recovery would.

What this proves, in order: the client saw a reset (the app is told the truth, not left hanging); the tape has the
request and some chunks and no terminal frame; `disconnects.from_tape` names it `INTERRUPTED_PROXY` and reports the bytes
the proxy had received; and the lease's own `is_stale()` says `pid gone` rather than "live", which is the half of the
class definition that a reader cannot get from the tape alone.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from blackbox import constants as C
from blackbox.proxy import disconnects
from blackbox.rec import recovery
from blackbox.tape import lease, scanner

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "_death_child.py"
PIECE = b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n'
CHUNKS = 400

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGKILL"),
                                reason="SIGKILL does not exist on Windows; S16 is POSIX-only by nature")


class _SlowOrigin(BaseHTTPRequestHandler):
    """Streams for long enough that the kill lands in the middle of a call, not before it or after it."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                    # http.server's spelling, not ours to rename
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(CHUNKS):
            self.wfile.write(f"{len(PIECE):x}\r\n".encode() + PIECE + b"\r\n")
            self.wfile.flush()
            time.sleep(0.005)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def slow_origin() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOrigin)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_a_killed_proxy_is_an_interrupted_proxy_and_the_tape_says_so(slow_origin: ThreadingHTTPServer,
                                                                     tmp_path: Path) -> None:
    data_dir = tmp_path / "run"
    data_dir.mkdir()
    upstream = f"http://127.0.0.1:{slow_origin.server_address[1]}"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(data_dir)}
    env.pop("BLACKBOX_DISABLED", None)
    proc = subprocess.Popen([sys.executable, str(CHILD), str(data_dir), upstream],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, "the child died before announcing its port"
        port = int(json.loads(line)["port"])

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(b"GET /p/stream HTTP/1.1\r\nHost: proxy\r\n\r\n")
        received = b""
        while len(received) < 2048:
            received += sock.recv(1024)
        assert received.startswith(b"HTTP/1.1 200"), received[:40]

        proc.kill()                                     # S16: the writer's process is gone, mid-response
        proc.wait(10)
        assert proc.returncode is not None

        # The client must be *told*, not left waiting: a reset, or an EOF with the body short of its framing.
        try:
            tail = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                tail += chunk
            assert not tail.rstrip().endswith(b"0\r\n\r\n"), "the stream must not look complete when the proxy died"
        except ConnectionError:
            pass                                        # a reset is the expected outcome
        finally:
            sock.close()
    finally:
        if proc.poll() is None:                         # a failing assertion above must not leak the child
            proc.kill()
            proc.wait(10)
        if proc.stdout is not None:
            proc.stdout.close()

    # 1. The lease says the writer is dead, and says *why*. `INTERRUPTED_PROXY` needs both halves of that sentence.
    written = lease.read(str(data_dir / "writer.lease"))
    assert written is not None, "the child acquired a lease; a dead writer that never did cannot be classified"
    stale, why = written.is_stale()
    assert stale and why == "pid gone", (stale, why)

    # 2. Recovery, from outside the dead process, classifies the call the way §10.3's row requires.
    result = recovery.recover(str(data_dir))
    assert result.node_statuses, result.to_dict()
    assert any(status["status"] == recovery.INTERRUPTED_MID_STREAM for status in result.node_statuses), \
        result.node_statuses
    scanned = scanner.scan_all(str(data_dir))
    assert scanned, "a killed writer leaves a tape directory behind, and it is readable"
    frames = [frame for scan in scanned for frame in scan.frames]

    # 3. The absence becomes a class, with the byte count the proxy had actually taken from the provider.
    classes = disconnects.from_tape(frames, writer_alive=not stale)
    assert [entry["class"] for entry in classes] == [disconnects.INTERRUPTED_PROXY], classes
    assert classes[0]["proxy_received_bytes"] > 0, "the proxy had relaid chunks before it died"
    assert classes[0]["client_received_bytes"] is None, "a dead proxy cannot report what the client received"
    kinds = [frame["kind"] for frame in frames]
    assert kinds.count(C.KIND_MODEL_REQUEST) == 1
    assert kinds.count(C.KIND_MODEL_CHUNK) > 0
    assert kinds.count(C.KIND_MODEL_RESPONSE) == 0 and kinds.count(C.KIND_MODEL_ERROR) == 0, \
        "no terminal frame is exactly what makes this INTERRUPTED_PROXY"
