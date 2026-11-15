"""P3B-08's client matrix: three clients that share nothing but the URL, all recorded without a line of their code.

The claim the proxy makes is universality — *any* language whose SDK honours a base-URL variable — and the only way to
test a claim about other people's code is to run other people's code. So: `curl` (libcurl, a separate process, no Python
in the loop), Node's `http` module (a different runtime entirely), and the published `openai` SDK (the client this product
exists for, streaming, with the recorder seeing the call).

Every one of them asserts the same two things: **the bytes are identical** to what the origin pushed, and **the proxy
knows the request happened** (its own counter, not the tape's). A client that is missing from the runner is skipped by
name, because "not installed here" is a fact about the runner and never about the proxy — `docs/compatibility.md` says
which cells run which.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _harness import Box

CURL = shutil.which("curl")
NODE = shutil.which("node")

SSE_LINE = b'data: {"id":"chatcmpl-m","choices":[{"delta":{"content":"tok"},"index":0}]}\n\n'
SSE_BODY = SSE_LINE * 24 + b"data: [DONE]\n\n"


class _SdkOrigin:
    """An origin that answers a chat completion the way a provider does — including `[DONE]`, which the SDK waits for."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        body = SSE_BODY
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:                        # http.server's spelling
                length = int(self.headers.get("content-length", "0"))
                requests.append({"path": self.path, "body": self.rfile.read(length)})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for index in range(0, len(body), 64):
                    piece = body[index:index + 64]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.requests = requests
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        import threading

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.mark.skipif(CURL is None, reason="curl is not installed in this runner")
def test_curl_gets_the_origins_bytes_through_the_proxy():
    with Box() as box:
        assert box.proxy is not None
        done = subprocess.run([CURL, "-sS", "--http1.1", "-N", "--max-time", "30",
                               f"http://127.0.0.1:{box.proxy.port}/p/stream"],
                              capture_output=True, check=True)
        assert hashlib.sha256(done.stdout).hexdigest() == hashlib.sha256(box.origin.stream_body).hexdigest(), \
            "libcurl received a different stream than the origin pushed"
        assert box.proxy.stats.requests == 1
        assert box.origin.requests[-1]["target"] == "/stream"


@pytest.mark.skipif(NODE is None, reason="node is not installed in this runner")
def test_node_gets_the_origins_bytes_through_the_proxy():
    with Box() as box:
        assert box.proxy is not None
        script = (
            "const http = require('http');\n"
            "http.get(process.argv[2], (res) => {\n"
            "  const chunks = [];\n"
            "  res.on('data', (c) => chunks.push(c));\n"
            "  res.on('end', () => { process.stdout.write(Buffer.concat(chunks).toString('base64')); });\n"
            "}).on('error', (e) => { process.stderr.write(String(e)); process.exit(1); });\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            done = subprocess.run([NODE, path, f"http://127.0.0.1:{box.proxy.port}/p/stream"],
                                  capture_output=True, check=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        got = base64.b64decode(done.stdout)
        assert hashlib.sha256(got).hexdigest() == hashlib.sha256(box.origin.stream_body).hexdigest()
        assert box.proxy.stats.requests == 1


def test_the_openai_sdk_streams_through_the_proxy_without_knowing_it_is_there():
    openai = pytest.importorskip("openai")
    origin = _SdkOrigin()
    try:
        import asyncio
        import threading

        from blackbox.proxy.routes import Route, Routes
        from blackbox.proxy.server import ProxyServer

        holder: dict[str, object] = {}
        ready = threading.Event()
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)

            async def start() -> None:
                routes = Routes([Route(name="openai", base=origin.base, prefix="/v1")], default=None)
                holder["proxy"] = await ProxyServer(routes).start()
                ready.set()

            loop.run_until_complete(start())
            loop.run_forever()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert ready.wait(10)
        proxy = holder["proxy"]
        try:
            client = openai.OpenAI(api_key="test-key", base_url=f"http://127.0.0.1:{proxy.port}/v1",  # type: ignore[attr-defined]
                                   max_retries=0, timeout=10.0)
            stream = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}],
                                                    stream=True)
            pieces = [chunk.choices[0].delta.content or "" for chunk in stream]
            assert "".join(pieces) == "tok" * 24
            assert proxy.stats.requests == 1                      # type: ignore[attr-defined]
            assert origin.requests[-1]["path"] == "/chat/completions", "the prefix is stripped, the path is kept"
            sent = json.loads(bytes(origin.requests[-1]["body"]).decode())
            assert sent["stream"] is True and sent["model"] == "m"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10)
            loop.close()
    finally:
        origin.close()


@pytest.mark.skipif(CURL is None or NODE is None, reason="the agreement test needs both clients in the runner")
def test_the_two_process_clients_disagree_about_nothing():
    """The point of a matrix is not three passes; it is one answer reached three ways."""
    node_script = (
        "const http = require('http');\n"
        "http.get(process.argv[2], (res) => {\n"
        "  const chunks = [];\n"
        "  res.on('data', (c) => chunks.push(c));\n"
        "  res.on('end', () => { process.stdout.write(Buffer.concat(chunks).toString('base64')); });\n"
        "}).on('error', (e) => { process.stderr.write(String(e)); process.exit(1); });\n"
    )
    with Box() as box:
        assert box.proxy is not None
        url = f"http://127.0.0.1:{box.proxy.port}/p/stream"
        got_curl = subprocess.run([CURL, "-sS", "--http1.1", "-N", "--max-time", "30", url],
                                  capture_output=True, check=True).stdout
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(node_script)
            path = handle.name
        try:
            got_node = base64.b64decode(subprocess.run([NODE, path, url], capture_output=True, check=True,
                                                       timeout=60).stdout)
        finally:
            Path(path).unlink(missing_ok=True)
        expected = hashlib.sha256(box.origin.stream_body).hexdigest()
        assert [hashlib.sha256(got_curl).hexdigest(), hashlib.sha256(got_node).hexdigest()] == [expected, expected]
        assert box.proxy.stats.requests == 2
