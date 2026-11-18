"""B4 / P3B-03: a node-to-node edge needs **two recorded spans**, and the proxy now records one.

Before this, a proxied call produced two records of one call — the app's `MODEL_REQUEST` inside an app node, and the
proxy's `MODEL_REQUEST` inside nothing — so the graph could say `level: "call"`, `node_join: "partial"`, and the honest
answer to "why did this step fail" stopped at the app's side of the boundary. The proxy now opens a span of its own for
every request it forwards: `NODE_OPEN` / … / `NODE_CLOSE` with a slot from the band `constants` reserves for the proxy
writer, `owner: "proxy"` on the open frame, and the call's frames carrying that slot.

What the edge may then say, and what these tests hold it to:

* **both ends exist because their writers said so** — the app node from the app tape's `NODE_OPEN`, the proxy span from
  the proxy tape's; neither is invented to give an edge an end;
* **the join is the call id both sides wrote**, never a time window, a fingerprint or a shared run id;
* **ambiguity is a refusal** — one id recorded twice on a side writes `level: "call"`, `node_join: "ambiguous"` and the
  candidate count, and names no node;
* **a missing half is a `call` edge** — an uninstrumented app (no seam) records no node, so the proxy span must not be
  promoted into one, and the summary keeps saying `partial`;
* **two writers on one tape cannot collide** — the hook folder's slots stop at `NODE_SLOT_HOOK_MAX`, the proxy's start at
  `NODE_SLOT_PROXY_BASE`, and the reserved band is asserted rather than assumed.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any

from blackbox import constants as C
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer
from blackbox.rec import links, recovery
from blackbox.tape import scanner
from blackbox.tape.writer import TapeWriter

BODY = b'{"id":"chatcmpl-1","choices":[{"message":{"content":"hi"}}]}'


class Emitted:
    """Collects what the sink emitted, so the frame order and each frame's slot can be asserted without a disk."""

    def __init__(self) -> None:
        self.frames: list[tuple[int, bytes, dict[str, Any]]] = []

    def __call__(self, kind: int, payload: bytes = b"", **fields: Any) -> int:
        self.frames.append((kind, payload, fields))
        return len(self.frames)

    def kinds(self) -> list[str]:
        return [C.KIND_NAMES[kind] for kind, _, _ in self.frames]

    def payloads(self) -> list[dict[str, Any]]:
        """Decoded JSON payloads, in frame order; a chunk frame's raw bytes decode to `{}` and are not the subject."""
        out: list[dict[str, Any]] = []
        for _, body, _ in self.frames:
            try:
                decoded = json.loads(body.decode() or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            out.append(decoded if isinstance(decoded, dict) else {})
        return out


def test_a_span_wraps_the_call_and_every_frame_carries_its_slot() -> None:
    """The frame order is the claim: open, request, chunks, response, close — all on one slot."""
    emit = Emitted()
    sink = TapeSink(emit, content="full")
    recorder = SinkRecorder(sink)
    sink.span_open(C.NODE_SLOT_PROXY_BASE, name="POST /v1/chat/completions", kind="proxy_call", owner="proxy",
                   route="openai", call_id="call-1")
    call = recorder.open_call("openai", "/v1/chat/completions", b"{}", call_id="call-1",
                              node_slot=C.NODE_SLOT_PROXY_BASE)
    recorder.receive(call, b"data: {}\n\n")
    recorder.close_call(call, status=200)
    sink.span_close(C.NODE_SLOT_PROXY_BASE, outcome="ok", status=200)

    assert emit.kinds() == ["NODE_OPEN", "MODEL_REQUEST", "MODEL_CHUNK", "MODEL_RESPONSE", "NODE_CLOSE"]
    slots = {fields.get("node_slot") for _, _, fields in emit.frames}
    assert slots == {C.NODE_SLOT_PROXY_BASE}, f"a frame escaped its span: {slots}"
    opened = emit.payloads()[0]
    assert opened["kind"] == "proxy_call" and opened["owner"] == "proxy", opened
    assert opened["call_id"] == "call-1" and opened["route"] == "openai", opened


class Origin:
    """A minimal upstream that answers after a short delay, so two requests are genuinely in flight together."""

    def __init__(self, delay_s: float = 0.2) -> None:
        self.delay_s = delay_s
        self.port = 0
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
                await asyncio.sleep(self.delay_s)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(BODY)).encode() + b"\r\nConnection: keep-alive\r\n\r\n" + BODY)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


class SpanBench:
    """One loop in one thread: an origin, a proxy whose receiver is a real `TapeSink` on a real tape."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.origin = Origin()
        self.proxy: ProxyServer | None = None
        self.writer: TapeWriter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.writer_uuid = ""
        self._closed = False
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SpanBench:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(15), f"the bench did not start: {self.error!r}"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(20)
        self.loop.call_soon_threadsafe(self.loop.stop)
        assert self._thread is not None
        self._thread.join(20)
        # The loop has to be shut down here, in this thread, and then closed: a loop left open with a pending task is
        # reported by a *later* test — on a Windows Proactor loop as "Task was destroyed but it is pending!" — which is
        # how this fixture failed a Windows CI cell while every assertion in it passed (2026-11-18).
        with contextlib.suppress(Exception):
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        with contextlib.suppress(Exception):
            self.loop.close()
        self._closed = True

    def _run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._start())
            self.ready.set()
            self.loop.run_forever()
        except BaseException as exc:                        # surfaced by the assertion in `__enter__`
            self.error = exc
            self.ready.set()

    async def _start(self) -> None:
        await self.origin.start()
        self.writer = TapeWriter(str(self.tmp_path), mode="process").open()
        sink = TapeSink(self.writer.emit, content="hash")
        table = Routes([Route(name="openai", base=f"http://127.0.0.1:{self.origin.port}")], default="openai")
        self.proxy = await ProxyServer(table, receiver=SinkRecorder(sink)).start()

    async def _stop(self) -> None:
        assert self.proxy is not None and self.writer is not None
        self.writer_uuid = self.writer.writer_uuid.hex()
        await self.proxy.close()
        await self.origin.close()
        # Cancel cooperatively until nothing is left: one pass leaves the tasks that were spawned by another task's
        # cancellation (an accept loop waking a connection handler), and those are what a Proactor loop then reports.
        for _ in range(5):
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if not pending:
                break
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.writer.close()

    @property
    def port(self) -> int:
        assert self.proxy is not None
        return self.proxy.port

    def frames(self) -> list[dict[str, Any]]:
        """The tape, read **after** the writer closed — which is what a reader of a capsule does.

        Reading while a writer is live found the last two frames of the second span still behind the head pointer
        (measured here, 2026-11-18: `MODEL_RESPONSE` and `NODE_CLOSE` for the second slot appeared only once `close()`
        ran). That is head publication, not a lost write, and a test that polls a live tape would be asserting on the
        publisher's window instead of on what the run recorded.
        """
        assert self._closed, "read the tape after the bench closes: a live head lags its own frames"
        assert self.writer_uuid, "the writer's identity is captured at stop; the bench did not start"
        return list(scanner.scan(str(self.tmp_path), self.writer_uuid).frames)


def _call(port: int, call_id: str | None, *, timeout: float = 20.0) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"content-type": "application/json"}
    if call_id:
        headers["x-blackbox-call"] = call_id
        headers["x-blackbox-run"] = "r_app"
    try:
        connection.request("POST", "/v1/chat/completions", body=b"{}", headers=headers)
        response = connection.getresponse()
        response.read()
        return int(response.status)
    finally:
        connection.close()


def test_two_concurrent_calls_get_two_spans_and_each_frame_is_in_its_own(tmp_path: Path) -> None:
    """The measurement this blocker needs: two spans, two call ids, no frame in the wrong span."""
    results: dict[str, int] = {}
    with SpanBench(tmp_path) as bench:
        def one(call_id: str) -> None:
            results[call_id] = _call(bench.port, call_id)

        threads = [threading.Thread(target=one, args=(f"call-{index}",)) for index in range(2)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        elapsed = time.monotonic() - started
    assert results == {"call-0": 200, "call-1": 200}, results
    assert elapsed < bench.origin.delay_s * 4, "the two calls were not concurrent enough to test concurrency"

    frames = bench.frames()
    opens = [f for f in frames if f["kind"] == C.KIND_NODE_OPEN]
    closes = [f for f in frames if f["kind"] == C.KIND_NODE_CLOSE]
    assert len(opens) == 2 and len(closes) == 2, (len(opens), len(closes))

    by_slot: dict[int, list[int]] = {}
    for frame in frames:
        slot = int(frame.get("node_slot") or 0)
        if slot:
            by_slot.setdefault(slot, []).append(frame["kind"])
    assert len(by_slot) == 2, by_slot
    for slot, kinds in by_slot.items():
        assert slot >= C.NODE_SLOT_PROXY_BASE, f"a proxy span took a slot outside its band: {slot}"
        assert kinds == [C.KIND_NODE_OPEN, C.KIND_MODEL_REQUEST, C.KIND_MODEL_CHUNK,
                         C.KIND_MODEL_RESPONSE, C.KIND_NODE_CLOSE], kinds

    # Each call id lands in exactly one span, and the two spans hold different ids: the id is what joins a proxy span
    # to an app node, so a span holding both would make every edge through it ambiguous.
    span_of_call: dict[str, int] = {}
    for frame in frames:
        if frame["kind"] != C.KIND_MODEL_REQUEST:
            continue
        payload = json.loads(frame["payload"].decode() or "{}")
        span_of_call[payload["call_id"]] = int(frame.get("node_slot") or 0)
    assert set(span_of_call) == {"call-0", "call-1"}, span_of_call
    assert len(set(span_of_call.values())) == 2, span_of_call


def test_the_span_reads_back_from_the_tape_as_a_named_node(tmp_path: Path) -> None:
    """Defect 75: the span's identity has to survive the tape, not just the emit callback.

    The sink writes JSON payloads and the writer's direct callers write CBOR, so the reader has to read both or a node
    arrives in `graph.json` as `{kind: None, name: None, owner: None}` — a node an edge could name without a reader being
    able to say what it was.
    """
    with SpanBench(tmp_path) as bench:
        assert _call(bench.port, "call-read-back") == 200
    nodes = recovery.graph_nodes(bench.frames())
    assert len(nodes) == 1, nodes
    node = nodes[0]
    assert node["name"] == "POST /v1/chat/completions", node
    assert node["kind"] == "proxy_call" and node["owner"] == "proxy", node
    assert node["node_slot"] >= C.NODE_SLOT_PROXY_BASE, node
    assert node["status"] == "closed" and node["outcome"] == "ok", node
    assert isinstance(node["duration_us"], int) and node["duration_us"] >= 0, node


def test_the_proxy_counts_the_spans_it_opened(tmp_path: Path) -> None:
    with SpanBench(tmp_path) as bench:
        assert _call(bench.port, "call-solo") == 200
        assert bench.proxy is not None
        stats = bench.proxy.stats.as_dict()
        assert stats["spans"] == 1 and stats["spans_capped"] == 0, stats


def test_a_recorded_app_node_and_a_recorded_proxy_span_are_a_node_edge() -> None:
    """Both ends are spans their own writers opened; the join is the call id and nothing else."""
    app = links.RunView.of("writer-app", "seam", "r_app", (
        links.CallView(writer_uuid="writer-app", vantage="seam", wall_ns=1, fingerprint="fp", run_id="r_app",
                       node_slot=3, call_id="call-1"),))
    proxy = links.RunView.of("writer-proxy", "proxy", "r_app", (
        links.CallView(writer_uuid="writer-proxy", vantage="proxy", wall_ns=2, fingerprint="fp2", run_id="r_app",
                       node_slot=C.NODE_SLOT_PROXY_BASE, call_id="call-1"),))

    records = links.resolve_calls(app, proxy)
    assert len(records) == 1
    assert (records[0].level, records[0].left_node, records[0].right_node) == (
        "node", 3, C.NODE_SLOT_PROXY_BASE), records[0]

    document = json.dumps({"call_links": [record.as_dict() for record in records]}).encode()
    edges, summary = recovery.graph_edges(document, "writer-app")
    assert summary["node_level"] == 1 and summary["call_level"] == 0, summary
    assert summary["node_join"] == "exact", summary
    edge = next(e for e in edges if e["level"] == "node")
    assert edge["nodes"] == {"from": 3, "to": C.NODE_SLOT_PROXY_BASE}, edge
    assert edge["evidence"] == {"rule": "same call id", "carried_by": "x-blackbox-call"}, edge
    assert edge["from"]["writer"] == "writer-app" and edge["to"]["writer"] == "writer-proxy"


def test_one_id_recorded_twice_on_a_side_is_never_a_node_edge() -> None:
    """Ambiguity is a refusal: no node is named, the candidate count is written, and the level stays `call`."""
    app = links.RunView.of("writer-app", "seam", "r_app", (
        links.CallView(writer_uuid="writer-app", vantage="seam", wall_ns=1, fingerprint="a", node_slot=3,
                       call_id="dup"),
        links.CallView(writer_uuid="writer-app", vantage="seam", wall_ns=2, fingerprint="b", node_slot=4,
                       call_id="dup")))
    proxy = links.RunView.of("writer-proxy", "proxy", "r_app", (
        links.CallView(writer_uuid="writer-proxy", vantage="proxy", wall_ns=3, fingerprint="c",
                       node_slot=C.NODE_SLOT_PROXY_BASE, call_id="dup"),))
    records = links.resolve_calls(app, proxy)
    assert records[0].ambiguous is True and records[0].candidates == 2
    assert records[0].level == "call"
    edges, summary = recovery.graph_edges(
        json.dumps({"call_links": [record.as_dict() for record in records]}).encode(), "writer-app")
    assert summary["node_level"] == 0 and summary["ambiguous"] == 1, summary
    assert edges[0]["nodes"] == {"from": None, "to": None}, edges[0]


def test_a_proxy_span_with_no_app_node_is_a_call_edge_not_a_node_edge() -> None:
    """An uninstrumented app records no node. The proxy's span must not be promoted into one."""
    app = links.RunView.of("writer-app", "seam", "r_app", (
        links.CallView(writer_uuid="writer-app", vantage="seam", wall_ns=1, fingerprint="fp", call_id="call-1",
                       node_slot=None),))
    proxy = links.RunView.of("writer-proxy", "proxy", "r_app", (
        links.CallView(writer_uuid="writer-proxy", vantage="proxy", wall_ns=2, fingerprint="fp2",
                       node_slot=C.NODE_SLOT_PROXY_BASE, call_id="call-1"),))
    records = links.resolve_calls(app, proxy)
    assert records[0].level == "call", records[0]
    edges, summary = recovery.graph_edges(
        json.dumps({"call_links": [record.as_dict() for record in records]}).encode(), "writer-app")
    assert summary["node_level"] == 0 and summary["node_join"] == "partial", summary
    assert edges[0]["nodes"]["to"] == C.NODE_SLOT_PROXY_BASE and edges[0]["nodes"]["from"] is None, edges[0]


def test_no_spans_means_no_node_edge_at_all() -> None:
    """The negative control: a tape with no `NODE_OPEN` yields no node, so nothing can be joined at node level."""
    app = links.RunView.of("writer-app", "seam", "r_app", (
        links.CallView(writer_uuid="writer-app", vantage="seam", wall_ns=1, fingerprint="fp", call_id="call-1"),))
    proxy = links.RunView.of("writer-proxy", "proxy", "r_app", (
        links.CallView(writer_uuid="writer-proxy", vantage="proxy", wall_ns=2, fingerprint="fp2", call_id="call-1"),))
    _, summary = recovery.graph_edges(
        json.dumps({"call_links": [r.as_dict() for r in links.resolve_calls(app, proxy)]}).encode(), "writer-app")
    assert summary["node_level"] == 0 and summary["node_join"] == "unknown", summary


def test_the_two_writers_on_one_tape_have_disjoint_slot_bands() -> None:
    """The banner is not a comment: the proxy cannot reach a hook's slot, and a hook cannot reach the proxy's."""
    assert C.NODE_SLOT_HOOK_MAX < C.NODE_SLOT_PROXY_BASE <= C.NODE_SLOT_PROXY_MAX
    assert C.NODE_SLOT_PROXY_MAX < 0xFFFF, "a proxy span must stay inside the 16-bit slot field"
    source = Path(__file__).resolve().parents[1] / "src" / "blackbox" / "hooks" / "inbox.py"
    assert "NODE_SLOT_HOOK_MAX" in source.read_text(), (
        "the hook folder must bound itself at the top of its band, not at 0xFFFF")


# ── defect 77: a complete body must not be recorded as a cut stream ────────────────────────────────────────────────
class _Truncating:
    """An upstream that promises 100 bytes and sends 10, then closes: the shape a cut response actually has."""

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> _Truncating:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int((self._server.sockets or ())[0].getsockname()[1])
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n")
            writer.write(b'{"partial')
            await writer.drain()
            writer.close()


def _responses(frames: list[dict[str, Any]], kind: int = C.KIND_MODEL_RESPONSE) -> list[dict[str, Any]]:
    """The call's closing frame as a dict: a `MODEL_RESPONSE`, or a `MODEL_ERROR` when the writer recorded a failure."""
    return [json.loads(frame["payload"]) for frame in frames if frame["kind"] == kind]


def test_a_body_that_ended_inside_its_content_length_is_not_a_partial_call(tmp_path: Path) -> None:
    """Defect 77: `partial: True` on every proxied call made `diagnose` report a cut stream for a complete 404."""
    with SpanBench(tmp_path) as bench:
        assert _call(bench.port, "call-complete") == 200
    responses = _responses(bench.frames())
    assert len(responses) == 1, responses
    assert responses[0]["partial"] is False, responses[0]
    assert responses[0]["ending"] == "terminated", responses[0]
    assert responses[0]["error"] is None, responses[0]


def test_a_body_that_stopped_before_its_content_length_is_partial_and_unverified(tmp_path: Path) -> None:
    """The other direction: a genuinely short body must not be recorded as complete either."""
    class Truncated(SpanBench):
        def __init__(self, tmp_path: Path) -> None:
            super().__init__(tmp_path)
            self.origin = _Truncating()                    # type: ignore[assignment]

    # The client sees the cut — that is the point of the case — and what it raises is not what is under test here.
    with Truncated(tmp_path) as bench, contextlib.suppress(Exception):
        _call(bench.port, "call-cut", timeout=20)
    responses = _responses(bench.frames(), C.KIND_MODEL_ERROR)
    assert len(responses) == 1, responses
    assert responses[0]["partial"] is True, responses[0]
    assert responses[0]["ending"] == "unverified", responses[0]
    assert responses[0]["end_reason"], responses[0]
    assert responses[0]["error"], "a cut body has to be recorded as an error, not as a quiet short answer"
