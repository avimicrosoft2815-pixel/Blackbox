"""Self-contained local mock server + BlackBox recording test.

Binds only to 127.0.0.1 (loopback). Never calls an external model provider.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockModel(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)

        reply = {
            "id": "mock-response",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello! This is a local mock response."
                },
                "finish_reason": "stop"
            }]
        }
        body = json.dumps(reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


CLIENT_CODE = """\
import json
import os
import urllib.request
from urllib.parse import urlparse

base = os.environ["OPENAI_BASE_URL"].rstrip("/")
host = urlparse(base).hostname
if host not in ("127.0.0.1", "localhost"):
    raise RuntimeError("Stopping: the endpoint is not local.")

payload = json.dumps({
    "model": "mock-model",
    "messages": [{"role": "user", "content": "Hello, mock model!"}]
}).encode("utf-8")

request = urllib.request.Request(
    base + "/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=30) as response:
    result = json.loads(response.read())
    print("Mock replied:", result["choices"][0]["message"]["content"])
"""


def main() -> int:
    with open("mock_client.py", "w", encoding="utf-8") as f:
        f.write(CLIENT_CODE)

    server = HTTPServer(("127.0.0.1", 0), MockModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    upstream = f"http://127.0.0.1:{server.server_port}"
    env = os.environ.copy()
    env.pop("OPENAI_BASE_URL", None)
    env.pop("OPENAI_API_KEY", None)

    print("Starting local mock and BlackBox...", flush=True)
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "blackbox", "run",
                "--upstream", f"openai={upstream}@/openai",
                "--", sys.executable, "mock_client.py",
            ],
            env=env,
        )
        print("\nTest command exit code:", result.returncode)
        return result.returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
