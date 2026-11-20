"""The response head the proxy writes has to describe the body it is about to relay (defects 109 and 110's neighbour).

`_client_head` decides three things for every proxied response: the length, the framing, and whether the client
connection can stay open. Each of them is a promise about bytes that have not been written yet, so a wrong one is a
client-visible lie — and it was: an upstream that streams with neither `Content-Length` nor `Transfer-Encoding` (a body
that ends when the connection closes, which is what a straight `BaseHTTPRequestHandler` SSE endpoint does, and what the
mock provider does) was answered with `Transfer-Encoding: chunked` over raw, unchunked bytes. `curl` refused it with
"Malformed encoding found in chunked-encoding"; the SDKs happened not to look (defect 109).

These tests hold the head to the body in both directions: no framing claim without framing, no `keep-alive` on a body
whose end is the connection's end, and no framing at all on a status that carries no body.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from blackbox.proxy.server import ProxyServer  # noqa: E402

HEAD = ProxyServer._client_head


def _head(headers: list[tuple[str, str]], *, status: int = 200, keep: bool = True) -> str:
    instance = object.__new__(ProxyServer)
    return HEAD(instance, status, "OK" if status == 200 else "", headers, keep).decode("latin-1")


def test_an_unframed_body_closes_the_client_connection_and_claims_no_framing():
    """The defect: `Transfer-Encoding: chunked` was written for a body the proxy relays raw."""
    head = _head([("content-type", "text/event-stream")], keep=True)
    assert "Transfer-Encoding" not in head, "the proxy relays this body raw; claiming chunked is a parse error for curl"
    assert "Content-Length" not in head
    assert "Connection: close" in head, "a body that ends at the connection's end needs the client told so"


def test_a_declared_length_is_forwarded_and_the_connection_may_stay_open():
    head = _head([("content-length", "17"), ("content-type", "application/json")], keep=True)
    assert "Content-Length: 17" in head
    assert "Transfer-Encoding" not in head
    assert "Connection: keep-alive" in head


def test_a_chunked_upstream_is_relayed_as_chunked():
    head = _head([("transfer-encoding", "chunked")], keep=True)
    assert "Transfer-Encoding: chunked" in head
    assert "Connection: keep-alive" in head
    assert "Content-Length" not in head


def test_a_bodyless_status_carries_no_framing_at_all():
    for status in (204, 304):
        head = _head([], status=status)
        assert "Transfer-Encoding" not in head, f"a {status} with chunked framing is a client's parse error"
        assert "Content-Length" not in head


def test_our_headers_and_hop_by_hop_headers_never_reach_the_client():
    head = _head([("x-blackbox-call", "abc"), ("connection", "close"), ("content-length", "2")])
    assert "x-blackbox-call" not in head
    assert "Connection: close" not in head.split("\r\n\r\n")[0].replace("Connection: keep-alive", ""), \
        "the upstream's own Connection header is hop-by-hop and is replaced, not forwarded"
    assert "Content-Length: 2" in head
