"""§14.4a: the proxy tells the three interruptions apart, and the tape says which one it was.

The class is not decoration. `INTERRUPTED_CLIENT` means the app died and the provider kept billing;
`INTERRUPTED_UPSTREAM` means the provider died and the app saw a truncated stream. Those are different bugs in different
code owned by different people, and the previous revision of this proxy recorded both as `upstream-truncated` — which
reads as the second even when it was the first. These tests are written from outside the proxy: a real client closes a
real socket, or a real origin closes one, and the assertion is on the recovered tape.

`INTERRUPTED_PROXY` is not written by anyone — the process that would write it is dead — so it is tested in
`test_proxy_death.py`, which kills the proxy and reads the tape it left behind.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
from pathlib import Path

import pytest
from _harness import TRUNCATE_DECLARED, TRUNCATE_SENT, Box, wait_for

from blackbox import constants as C
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.proxy import disconnects
from blackbox.tape import scanner
from blackbox.tape import writer as writer_module


def _tape(tmp_path: Path) -> tuple[writer_module.TapeWriter, SinkRecorder]:
    """A real writer behind a real sink: the tape, not a captured list of calls, is what these tests read."""
    tape = writer_module.TapeWriter(str(tmp_path / "tape"), segment_bytes=256 * 1024).open()
    return tape, SinkRecorder(TapeSink(tape.emit, content="full", coalesce_bytes=1))


def _error_lines(frames: list[dict]) -> list[str]:
    """The class lines, read the way a report will read them: `disconnects.error_of` over recovered frames."""
    return [line for line in (disconnects.error_of(f) for f in frames if f.get("kind") == C.KIND_MODEL_ERROR)
            if line]


# ── classification, without a socket ───────────────────────────────────────────────────────────────────────────────

def test_the_class_a_live_proxy_proves_comes_from_the_side_that_closed_first():
    client = disconnects.classify(closed_first="client", status=0, client_received_bytes=1024, proxy_received_bytes=8192)
    upstream = disconnects.classify(closed_first="upstream", status=200, client_received_bytes=512,
                                    proxy_received_bytes=512)
    assert client.klass == "INTERRUPTED_CLIENT" and upstream.klass == "INTERRUPTED_UPSTREAM"
    assert client.client_received_bytes == 1024 and client.proxy_received_bytes == 8192


def test_the_error_line_carries_both_counts_and_reads_back():
    line = disconnects.classify(closed_first="client", status=0, client_received_bytes=1024,
                                proxy_received_bytes=8192).as_error()
    assert line.startswith("INTERRUPTED_CLIENT")
    assert disconnects.parse(line) == "INTERRUPTED_CLIENT", "a reader five months later can still name the class"
    assert disconnects.counts(line) == (1024, 8192), "and quote what each side actually moved"


def test_an_uncertain_outcome_is_not_filed_under_a_disconnect_class():
    """`upstream-closed-uncertain` is a different statement — we do not know what happened — and is not a disconnect."""
    assert disconnects.parse("upstream-closed-uncertain") is None
    assert disconnects.parse(None) is None
    assert disconnects.parse("") is None


# ── INTERRUPTED_CLIENT, over a real socket ─────────────────────────────────────────────────────────────────────────

def test_a_client_that_hangs_up_mid_stream_is_classified_as_the_client_and_not_the_provider(tmp_path: Path):
    tape, recorder = _tape(tmp_path)
    box = Box(receiver=recorder)
    try:
        with box:
            assert box.proxy is not None
            sock = socket.create_connection(("127.0.0.1", box.proxy.port), timeout=10)
            sock.sendall(b"GET /p/stream HTTP/1.1\r\nHost: proxy\r\n\r\n")
            received = b""
            while len(received) < 2048:
                received += sock.recv(1024)
            sock.close()                       # the app dies here; the model has not finished talking
            assert wait_for(lambda: box.proxy.stats.client_gone == 1), box.proxy.stats.as_dict()
        tape.publish()
    finally:
        tape.close()

    result = scanner.scan(str(tmp_path / "tape"), tape.writer_uuid.hex())
    errors = _error_lines(result.frames)
    assert len(errors) == 1 and errors[0].startswith("INTERRUPTED_CLIENT"), errors
    client_bytes, proxy_bytes = disconnects.counts(errors[0])
    # The two numbers are equal here on purpose, and that is not a bug in the classification: the proxy writes each
    # chunk it reads, so the chunk that broke the client's socket was read and written before the peer said so. What
    # the *class* adds is which side went away; what the *cost* is appears against the provider — it kept producing
    # after nobody was listening, which is the number §14.4a exists to make visible.
    assert 0 < client_bytes <= proxy_bytes, (client_bytes, proxy_bytes)
    # The cost is against the *provider*, which is the side that keeps billing: it had 512 chunks to send and was cut
    # off part-way, so its own tally is at least what the client was credited with and its stream never finished.
    assert not box.origin.stream_finished.is_set(), "the origin was cut off mid-stream: that is the cost of a dead client"
    assert box.origin.sent_stream_bytes >= client_bytes, (box.origin.sent_stream_bytes, client_bytes)
    assert box.origin.sent_stream_bytes < len(box.origin.stream_body), "the origin did not finish its stream"
    assert disconnects.parse(errors[0]) == disconnects.INTERRUPTED_CLIENT
    assert box.proxy is not None and box.proxy.stats.upstream_gone == 0


def test_the_proxy_keeps_serving_after_a_client_vanishes(tmp_path: Path):
    box = Box()
    with box:
        assert box.proxy is not None
        sock = socket.create_connection(("127.0.0.1", box.proxy.port), timeout=10)
        sock.sendall(b"GET /p/stream HTTP/1.1\r\nHost: proxy\r\n\r\n")
        sock.recv(256)
        sock.close()
        assert wait_for(lambda: box.proxy.stats.client_gone == 1)
        status, _headers, body = _plain(box.proxy.port)
        assert status == 200 and body, "a client's death is not the listener's"
        assert box.proxy.stats.requests == 2


# ── INTERRUPTED_UPSTREAM, over a real socket ────────────────────────────────────────────────────────────────────────

def test_an_upstream_that_dies_mid_body_is_classified_as_the_upstream(tmp_path: Path):
    tape, recorder = _tape(tmp_path)
    box = Box(receiver=recorder)
    try:
        with box:
            assert box.proxy is not None
            connection = http.client.HTTPConnection("127.0.0.1", box.proxy.port, timeout=10)
            connection.request("GET", "/p/truncate")
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("content-length") == str(TRUNCATE_DECLARED)
            with pytest.raises(http.client.IncompleteRead):
                response.read()                 # the client is still listening when the provider stops talking
            connection.close()
            assert wait_for(lambda: box.proxy.stats.upstream_gone == 1), box.proxy.stats.as_dict()
            assert box.proxy.stats.client_gone == 0
        tape.publish()
    finally:
        tape.close()

    result = scanner.scan(str(tmp_path / "tape"), tape.writer_uuid.hex())
    errors = _error_lines(result.frames)
    assert len(errors) == 1 and errors[0].startswith("INTERRUPTED_UPSTREAM"), errors
    client_bytes, proxy_bytes = disconnects.counts(errors[0])
    assert client_bytes == proxy_bytes == TRUNCATE_SENT, "both sides got the same short body; the client was not gone"


# ── reading a pair of tapes, which is what §14.4a is for ───────────────────────────────────────────────────────────

def test_two_tapes_that_disagree_about_one_call_are_itself_the_evidence():
    """The shape `explain` will report: the same call, two vantages, two byte counts."""
    client_line = disconnects.classify(closed_first="client", status=0, client_received_bytes=1024,
                                       proxy_received_bytes=8192).as_error()
    as_dict = disconnects.Disconnect(klass="INTERRUPTED_CLIENT", client_received_bytes=1024,
                                     proxy_received_bytes=8192)
    assert as_dict.as_dict() == {"class": "INTERRUPTED_CLIENT", "status": 0, "client_received_bytes": 1024,
                                 "proxy_received_bytes": 8192, "detail": ""}
    assert disconnects.parse(client_line) == as_dict.klass
    assert hashlib.sha256(client_line.encode()).hexdigest() == hashlib.sha256(
        disconnects.Disconnect(klass="INTERRUPTED_CLIENT", client_received_bytes=1024,
                               proxy_received_bytes=8192).as_error().encode()).hexdigest()


def test_from_tape_does_not_call_a_live_writers_open_call_an_interruption():
    frames = [{"kind": C.KIND_MODEL_REQUEST, "node_slot": 1}]
    assert disconnects.from_tape(frames, writer_alive=True) == [], "an in-flight call is not an interrupted one"
    dead = disconnects.from_tape(frames, writer_alive=False)
    assert [entry["class"] for entry in dead] == ["INTERRUPTED_PROXY"]


def test_from_tape_reads_a_written_class_and_leaves_a_completed_call_alone():
    frames = [
        {"kind": C.KIND_MODEL_REQUEST, "node_slot": 1},
        {"kind": C.KIND_MODEL_CHUNK, "node_slot": 1, "payload": b"abcd"},
        {"kind": C.KIND_MODEL_ERROR, "node_slot": 1,
         "error": "INTERRUPTED_UPSTREAM: client received 4 B, proxy received 4 B from the upstream; provider died"},
        {"kind": C.KIND_MODEL_REQUEST, "node_slot": 2},
        {"kind": C.KIND_MODEL_CHUNK, "node_slot": 2, "payload": b"abcd"},
        {"kind": C.KIND_MODEL_RESPONSE, "node_slot": 2, "error": None},
    ]
    entries = disconnects.from_tape(frames, writer_alive=False)
    assert [entry["class"] for entry in entries] == ["INTERRUPTED_UPSTREAM"]
    assert entries[0]["client_received_bytes"] == 4 and entries[0]["proxy_received_bytes"] == 4


def _plain(port: int) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/p/ok")
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def test_the_stats_name_the_two_sides_separately():
    """A counter each, because a single `upstream_errors` number is what hid this defect in the first place."""
    box = Box()
    with box:
        assert box.proxy is not None
        stats = box.proxy.stats.as_dict()
        assert "client_gone" in stats and "upstream_gone" in stats
        assert json.dumps(stats)               # serialisable: the launcher prints it and the bench ships it
