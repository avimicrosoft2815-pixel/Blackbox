"""A mock model provider that answers, then cuts the stream — the controlled interruption in the demo incident.

It speaks just enough of the OpenAI chat-completions SSE shape for a real client to parse, and it is deliberately hostile
in one specific way: after three deltas it closes the socket without sending `[DONE]`. That is the failure the demo film is
about — a response that *starts* and never finishes — and it happens locally, with no key, no account and no network.

Synthetic only: the "order" ids, the tool name and every token below are invented for the demo.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNKS = [
    {"tool_calls": [{"index": 0, "id": "call_demo_7f3a", "type": "function",
                     "function": {"name": "lookup_order", "arguments": '{"order_id": "ord_demo_4412"'}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": '", "scope": "status"'}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": '"summary": true}'}}]},
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:      # the film does not need the access log
        return

    def do_POST(self) -> None:                                   # noqa: N802 - http.server's spelling
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")     # what a real provider streams an SSE body with
        self.end_headers()
        for index, chunk in enumerate(CHUNKS):
            event = {"id": "chatcmpl-demo", "object": "chat.completion.chunk",
                     "model": "demo-model", "choices": [{"index": 0, "delta": chunk, "finish_reason": None}]}
            payload = f"data: {json.dumps(event)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()
            threading.Event().wait(0.12)
        print(f"mock-provider: served {len(CHUNKS)} deltas, then dropped the connection without [DONE]", flush=True)
        self.close_connection = True
        try:
            self.connection.shutdown(2)                          # the interruption itself
        except OSError:
            pass

    def do_GET(self) -> None:                                    # noqa: N802
        body = b'{"ok": true, "note": "mock provider for the BlackBox demo film"}\n'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8787
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock-provider: listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
