"""B5: which OTLP importers actually accept a capsule's export, measured — and which ones this sandbox cannot run.

B5's sentence is "test additional OTLP importers where feasible, recording exact versions, setup, results; mark
unavailable/untested integrations honestly". So this script has two halves and keeps them apart:

* **Measured here.** The capsule's own bytes are handed to importers that do run in this environment. For each one the
  artefact records the exact package versions, the setup, what came back and what was read out of it afterwards.
* **Not measurable here.** Every importer that needs a binary, a database or a container is listed with the reason and the
  command that failed, so the gate stays open rather than being closed by a footnote.

What runs, in order:

1. **The official protobuf schema** (`opentelemetry-proto`, the generated messages OTel publishes) decodes
   `otlp/spans.pb` and `otlp/spans.json` (proto3 JSON). This is a schema conformance check, not an importer: it proves the
   bytes are the message OTel defines, including field numbers and the JSON mapping, which is the part a hand-written
   decoder can get wrong in the same way twice.
2. **A gRPC OTLP receiver on 4317**, built from that package's `TraceServiceServicer` and `grpcio`: the capsule's bytes go
   over OTLP/gRPC exactly as an exporter would send them, the servicer decodes them with OTel's own parser, and what it saw
   is compared with what the capsule says. Phoenix's *HTTP* receiver was measured on 2026-11-16
   (`bench/results/2026-11-16-otlp-pb-importer.json`); the gRPC path was the untested one, and this is the transport test
   without a product attached to it.
3. **The capsule's own `capsule verify`**, because an importer accepting bytes that the capsule itself would call drifted
   is not a pass.

    python bench/bench_otlp_importers.py --out bench/results/2026-11-18-otlp-importers.json

stdlib plus the optional packages it records; the loopback listener is on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import tempfile
import time
from concurrent import futures
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import writer as writer_mod


def _version(dist: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:
        return None


def _build_capsule(work: Path) -> tuple[Path, dict[str, Any]]:
    """A real tape and its real capsule: the same frames `tests/test_export_otlp.py` uses, written by the real writer."""
    data = work / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1).open()
    run_id = "r_0123456789abcdef"
    request = json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions", "model": "gpt-4",
                          "call_id": "c0ffee"}).encode()
    response = json.dumps({"provider": "api.openai.com", "path": "/v1/chat/completions", "model": "gpt-4",
                           "status": 200, "call_id": "c0ffee", "chunks_raw": 3,
                           "bytes_received": 42}).encode()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": run_id}), node_slot=0)
    writer.emit(C.KIND_NODE_OPEN, cbor.encode({"name": "agent", "kind": "run"}), node_slot=1)
    writer.emit(C.KIND_MODEL_REQUEST, request, node_slot=1)
    writer.emit(C.KIND_MODEL_RESPONSE, response, node_slot=1)
    writer.emit(C.KIND_NODE_CLOSE, cbor.encode({"name": "agent", "outcome": "ok"}), node_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": run_id}), node_slot=0)
    writer.close()
    result = recovery.recover(str(data), writer.writer_uuid.hex())
    if result is None or not result.capsule_path:
        raise RuntimeError("the tape did not recover into a capsule")
    path = Path(result.capsule_path)
    capsule = cap.read_capsule(str(path))
    # `cap.OTLP_NAME` is the JSON part; the protobuf part has its own name, and mixing the two is exactly the kind of
    # mistake that makes a "the importer accepted it" claim meaningless (measured here: the JSON bytes handed to the
    # protobuf parser gave `Wire format was corrupt`).
    pb = capsule.entry(cap.OTLP_PB_NAME)
    document = json.loads(capsule.entry(cap.OTLP_NAME))
    summary = {"path": str(path), "capsule_id": path.name, "pb_bytes": len(pb),
               "pb_sha256": __import__("hashlib").sha256(pb).hexdigest(),
               "parts": sorted(capsule.manifest.get("parts") or []) if capsule.manifest else [],
               "json_spans": len(document["resourceSpans"][0]["scopeSpans"][0]["spans"]),
               "json_trace_id": document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"],
               "json_names": [span["name"] for span in document["resourceSpans"][0]["scopeSpans"][0]["spans"]]}
    return path, summary, pb, document


def _protobuf_schema(pb: bytes, document: dict) -> dict[str, Any]:
    """Decode the capsule's own bytes with the messages OTel publishes, and parse the JSON as proto3 JSON."""
    try:
        from google.protobuf import json_format
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"available": True, "binary": {}, "json": {}}
    try:
        message = trace_service_pb2.ExportTraceServiceRequest()
        message.ParseFromString(pb)
        spans = [span for resource in message.resource_spans for scope in resource.scope_spans for span in scope.spans]
        out["binary"] = {"accepted": True, "spans": len(spans), "names": [span.name for span in spans],
                         "trace_ids": sorted({span.trace_id.hex() for span in spans}),
                         "parents_resolve": all(
                             (not span.parent_span_id) or any(other.span_id == span.parent_span_id for other in spans)
                             for span in spans)}
    except Exception as exc:
        out["binary"] = {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        parsed = trace_service_pb2.ExportTraceServiceRequest()
        json_format.Parse(json.dumps(document), parsed, ignore_unknown_fields=False)
        spans = [span for resource in parsed.resource_spans for scope in resource.scope_spans for span in scope.spans]
        out["json"] = {"accepted": True, "spans": len(spans), "names": [span.name for span in spans]}
    except Exception as exc:
        out["json"] = {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _grpc_receiver(pb: bytes, document: dict) -> dict[str, Any]:
    """An OTLP/gRPC `TraceService` on 4317 built from OTel's servicer: the transport the HTTP receiver never tested."""
    try:
        import grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    seen: list[dict[str, Any]] = []

    class Receiver(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):                     # the name the .proto gives it
            spans = [span for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            seen.append({"spans": len(spans), "names": [span.name for span in spans],
                         "trace_ids": sorted({span.trace_id.hex() for span in spans}),
                         "partial_success": bool(request.resource_spans)})
            return trace_service_pb2.ExportTraceServiceResponse()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Receiver(), server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    out: dict[str, Any] = {"available": True, "port": port, "transport": "OTLP/gRPC (application/grpc, protobuf)"}
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = trace_service_pb2_grpc.TraceServiceStub(channel)
            message = trace_service_pb2.ExportTraceServiceRequest()
            message.ParseFromString(pb)
            response = stub.Export(message, timeout=10)
            out["status"] = "OK"
            out["response_has_partial_success_field"] = hasattr(response, "partial_success")
        out["received"] = seen[0] if seen else None
    except Exception as exc:
        out["status"] = f"FAILED: {type(exc).__name__}: {exc}"
    finally:
        server.stop(0)
    return out


def _unavailable() -> list[dict[str, Any]]:
    """What this sandbox cannot run, with the command that failed rather than an adjective."""
    return [
        {"name": "OpenTelemetry Collector (otelcol)", "why": "needs the released Go binary",
         "evidence": "curl -sIL https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
                     "v0.115.0/otelcol_0.115.0_linux_amd64.tar.gz -> 302 to objects.githubusercontent.com, which this "
                     "sandbox cannot reach (download stopped at 0 bytes)",
         "would_measure": "otelcol --config with an otlp receiver and the debug exporter, then POST the capsule's bytes"},
        {"name": "Langfuse", "why": "server is a TypeScript app with Postgres behind it",
         "evidence": "pip has no server package; the docker-compose deployment needs images this sandbox cannot pull",
         "would_measure": "POST /api/public/otel/v1/traces with basic auth, read the trace back over the public API"},
        {"name": "Grafana Tempo", "why": "Go binary", "evidence": "same release-asset block as otelcol",
         "would_measure": "tempo -config.file=... and the trace-by-id HTTP API"},
        {"name": "Jaeger", "why": "Go binary", "evidence": "same release-asset block as otelcol",
         "would_measure": "jaeger all-in-one with the OTLP receiver on 4318 and its /api/traces endpoint"},
        {"name": "Phoenix, OTLP/gRPC on 4317", "why": "a real Phoenix server is a large optional install and its gRPC "
                                                       "listener was not up in the 2026-11-16 HTTP measurement",
         "evidence": "bench/results/2026-11-16-otlp-pb-importer.json measured the HTTP receiver only",
         "would_measure": "phoenix serve, then the gRPC stub in this script pointed at 127.0.0.1:4317"},
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B5: record which OTLP importers accept a capsule's export")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "2026-11-18-otlp-importers.json"))
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="bbx-otlp-"))
    capsule_path, summary, pb, document = _build_capsule(work)
    schema = _protobuf_schema(pb, document)
    grpc_result = _grpc_receiver(pb, document)

    report = {
        "artifact": "OTLP importers: what accepted the capsule's bytes here, and what could not be run",
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "ticket": "B5 (P4-14/P4-20's importer clause)",
        "environment": {
            "platform": platform.platform(), "python": platform.python_version(),
            "packages": {"opentelemetry-proto": _version("opentelemetry-proto"),
                         "protobuf": _version("protobuf"), "grpcio": _version("grpcio"),
                         "arize-phoenix": _version("arize-phoenix")},
        },
        "capsule": {"built": summary, "verify": None},
        "results": {"official_protobuf_schema": schema, "otlp_grpc_receiver": grpc_result},
        "not_measured": _unavailable(),
        "previous_measurement": "bench/results/2026-11-16-otlp-pb-importer.json (Phoenix 21.0.0, HTTP/protobuf: "
                                "spans.pb 200, spans.json 415)",
    }

    # The capsule has to verify itself for any of the above to be worth recording.
    import contextlib
    import io

    from blackbox.cap.cli import main as capsule_main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            code = capsule_main(["verify", str(capsule_path)])
        except SystemExit as exc:                               # a CLI that exits is still a result
            code = exc.code if isinstance(exc.code, int) else 1
    report["capsule"]["verify"] = {"exit_code": code, "output": buffer.getvalue().strip().splitlines()[-6:]}

    binary = schema.get("binary") or {}
    report["verdict"] = {
        "capsule_verifies": code == 0,
        "otlp_binary_is_the_official_message": bool(binary.get("accepted")),
        "otlp_json_parses_as_proto3_json": bool((schema.get("json") or {}).get("accepted")),
        "grpc_receiver_accepted_it": grpc_result.get("status") == "OK",
        "names_match_the_capsule": (binary.get("names") or []) == summary["json_names"],
        "importers_measured": [name for name, value in (("official schema", schema), ("grpc receiver", grpc_result))
                               if value.get("available")],
        "importers_unmeasured": [row["name"] for row in _unavailable()],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("capsule", summary["capsule_id"], summary["pb_bytes"], "bytes,", summary["json_spans"], "spans")
    print("verify exit", code)
    print("schema:", json.dumps(schema, sort_keys=True)[:400])
    print("grpc:", json.dumps(grpc_result, sort_keys=True)[:400])
    print("verdict:", json.dumps(report["verdict"], sort_keys=True))
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
