"""The synthetic application in the demo incident: an order assistant that calls a model, then acts on the answer.

It is written with the standard library only and it talks to whatever `OPENAI_BASE_URL` names — which, under
`blackbox run`, is the recorder's loopback proxy. Every value here is invented; there is no customer, no order and no
account behind any of it.

The failure is real in the sense that matters: the provider stops mid-stream, the client raises the exception a real client
would raise, and the process exits non-zero while a tool call it *started* has no recorded result.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ORDER_ID = "ord_demo_4412"
QUESTION = "Where is order ord_demo_4412, and can it still be changed?"


def stream_completion(base_url: str, timeout: float = 10.0) -> str:
    """Ask the model for a tool call, and return the raw stream. Raises when the stream ends without `[DONE]`."""
    payload = json.dumps({
        "model": "demo-model",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are an order desk. Use tools when they exist."},
            {"role": "user", "content": QUESTION},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup_order",
            "description": "Read an order's status from the order service",
            "parameters": {"type": "object", "properties": {
                "order_id": {"type": "string"}, "scope": {"type": "string"}}}}}],
    }).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=payload, method="POST",
        headers={"content-type": "application/json", "authorization": "Bearer demo-not-a-key"})
    raw = ""
    with urllib.request.urlopen(request, timeout=timeout) as response:      # noqa: S310 - loopback only
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            body = text[5:].strip()
            if body == "[DONE]":
                return raw
            raw += body
    # The provider closed the stream without finishing the message: an unanswered tool call, not an empty answer.
    raise ConnectionError("upstream closed the stream before [DONE]: the assistant message is incomplete")


def main() -> int:
    base_url = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8787/v1")
    print(f"order-desk: asking for {ORDER_ID}")
    try:
        stream = stream_completion(base_url)
    except (ConnectionError, urllib.error.URLError, TimeoutError) as exc:
        # This is the moment the run dies: the model had already decided to call `lookup_order`, and the tool's result was
        # never obtained, let alone recorded. Whether the order was read is — honestly — unknown.
        print(f"order-desk: failed while reading the response: {exc}", file=sys.stderr)
        return 3
    print(f"order-desk: finished with {len(stream)} bytes of assistant message")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
