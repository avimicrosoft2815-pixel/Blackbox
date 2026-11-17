"""`events/events.jsonl` — the capsule's timeline as one JSON object per frame (P4-13).

The raw segments are the authoritative artefact and they are CBOR-headed binary: readable by this project's reader and by
nothing else. `events.jsonl` is the *derived* form of the same bytes for tools people already have, and it is a derived
part in the plan's sense (§13.1a): `manifest.derived_parts[]` carries its sha256, `blackbox capsule verify` recomputes it
from the segments and reports drift as `BBX-E412`, and nothing ever rewrites it.

Three rules govern what a line may say.

1. **A name is used only where it exists.** The `gen_ai.*` names come from OpenTelemetry's semantic conventions, pinned
   here to **v1.39.0** (`6d05e92`, read from the upstream repository): the provider attribute is `gen_ai.provider.name`
   (it was `gen_ai.system` before v1.33), and `gen_ai.operation.name`'s well-known values include `chat`,
   `text_completion`, `embeddings`, `invoke_agent` and `execute_tool`. Nothing else is invented: a frame whose payload
   does not carry a model, or whose path is not one of the APIs the mapping knows, gets no `gen_ai.*` key at all rather
   than a plausible-looking guess. `cap/export_otlp.py` is pinned to the same tag, so the two exports cannot describe the
   same run with two different vocabularies.
2. **Everything else is `bbx.`.** Every attribute is either a pinned semconv name or a name this project owns, so a
   reader can tell at a glance which claims are standardised and which are ours.
3. **The export is a timeline, not a second tape.** Chunk *content* is not copied into it: a `MODEL_CHUNK` line carries
   the byte count (and the `[bbx-…]` header when the sink tagged it), so the file stays bounded and reviewable while the
   bytes stay in the segments where the content policy put them.

Byte-identical regeneration is the requirement that shapes the rest: the file is a pure function of the frames — no
generation timestamp, no capsule id, sorted keys, `", "`-free separators, LF, and a final newline. Rendering the same
frames twice gives the same bytes, which is what makes `capsule verify` a comparison rather than a hope.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from blackbox import constants as C
from blackbox.codec import cbor

#: The semantic-conventions tag these names were read from, and the commit it points at. Recorded in the export's own docs
#: and in `format.md`, because a name without a version is a name that will mean something else next year.
SEMCONV = "v1.39.0"
SEMCONV_COMMIT = "6d05e92070f0a5c9a5bbcd396d2ebe2103377f8c"

#: Path suffix -> `gen_ai.operation.name`. Only the APIs the mapping actually knows; a path that is not here gets no
#: operation name, which is a fact the reader can see.
OPERATIONS: tuple[tuple[str, str], ...] = (
    ("/chat/completions", "chat"),          # OpenAI chat
    ("/messages", "chat"),                  # Anthropic messages
    ("/api/chat", "chat"),                  # Ollama chat
    ("/completions", "text_completion"),    # OpenAI legacy completions
    ("/api/generate", "text_completion"),   # Ollama generate
)

#: Host -> `gen_ai.provider.name` (v1.39.0's well-known values). A host that is not a provider we can name gets no
#: `gen_ai.provider.name`: the recorder knows the address it dialled, not who answers there. The list is explicit
#: because it must be — a suffix match would turn `not-openai.example` into `openai`.
SYSTEMS: dict[str, str] = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.cohere.ai": "cohere",
    "api.cohere.com": "cohere",
    "api.groq.com": "groq",
    "api.deepseek.com": "deepseek",
    "api.mistral.ai": "mistral_ai",
    "api.x.ai": "x_ai",
    "api.perplexity.ai": "perplexity",
    "generativelanguage.googleapis.com": "gcp.gemini",
    "aiplatform.googleapis.com": "gcp.vertex_ai",
}

#: Kinds whose payload is decoded into attributes. Everything else keeps its payload under `bbx.payload`.
_MODEL_KINDS = (C.KIND_MODEL_REQUEST, C.KIND_MODEL_RESPONSE, C.KIND_MODEL_ERROR)
_PAYLOAD_LIMIT = 2048


def operation_of(path: str | None) -> str | None:
    """The semconv operation name for a path, or `None` when the mapping does not know that API."""
    text = str(path or "")
    for suffix, name in OPERATIONS:
        if text.endswith(suffix):
            return name
    return None


def system_of(provider: str | None) -> str | None:
    """The semconv `gen_ai.provider.name` for a recorded host, or `None` — never a guess."""
    host = str(provider or "").split(":", 1)[0].strip().lower()
    return SYSTEMS.get(host)


def _payload_of(frame: dict[str, Any]) -> Any:
    """A frame's payload as a value: JSON first (model frames), then CBOR (the rest), else `None`."""
    raw = frame.get("payload") or b""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        pass
    try:
        return cbor.decode_exact(bytes(raw))
    except Exception:
        return None


def _model_attributes(payload: Any, kind: int) -> dict[str, Any]:
    """The attributes a model frame justifies, `gen_ai.*` where the pinned mapping has a name for it."""
    out: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out
    path, provider, model = payload.get("path"), payload.get("provider"), payload.get("model")
    operation = operation_of(path if isinstance(path, str) else None)
    if operation and kind == C.KIND_MODEL_REQUEST:
        out["gen_ai.operation.name"] = operation
    system = system_of(provider if isinstance(provider, str) else None)
    if system:
        out["gen_ai.provider.name"] = system
    if isinstance(model, str) and model:
        out["gen_ai.request.model"] = model
    # Ours, because the mapping has no name for them: the vantages, the id, the fingerprints and the counters.
    if isinstance(provider, str) and provider:
        out["bbx.provider.host"] = provider
    for key in ("request_fp", "response_fp", "path", "capture_seam", "content", "stream", "partial",
                "linked_run", "call_id", "status", "chunks_events", "chunks_raw", "chunks_stored",
                "bytes_stored", "bytes_received", "first_byte_ms", "total_ms", "error"):
        if key in payload and payload[key] is not None:
            out[f"bbx.model.{key}"] = _jsonable(payload[key])
    redaction = payload.get("redaction")
    if isinstance(redaction, dict):
        # Counts only — the redactor's own summary, never its input.
        out["bbx.redaction"] = {key: _jsonable(redaction.get(key))
                                for key in ("mode", "counts", "failures") if key in redaction}
    return out


def _jsonable(value: Any) -> Any:
    """A payload value as JSON can carry it: bytes become a tagged base64 string, never a Python repr.

    A `CHAIN` frame's payload holds raw hashes and a `GAP`'s holds a chain value, so this is not a corner case: `bytes`
    reaching the encoder is the difference between a derived part that renders and one that raises inside a seal.
    """
    if isinstance(value, (bytes, bytearray)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _attributes(frame: dict[str, Any]) -> dict[str, Any]:
    """Everything one frame says, as attribute names: semconv where pinned, `bbx.` otherwise."""
    kind = int(frame.get("kind") or 0)
    payload = _payload_of(frame)
    if kind in _MODEL_KINDS:
        return _model_attributes(payload, kind)
    if kind == C.KIND_MODEL_CHUNK:
        raw = bytes(frame.get("payload") or b"")
        # A timeline, not a second tape: the bytes stay in the segments, this says how many there were and whether the
        # sink tagged them (`[bbx-hash …]` / `[bbx-off …]`), which is what a reader needs to count.
        out: dict[str, Any] = {"bbx.chunk.bytes": len(raw)}
        if raw.startswith(b"[bbx-") and raw.endswith(b"]") and len(raw) <= 160:
            out["bbx.chunk.tag"] = raw.decode("ascii", "replace")
        return out
    if kind in (C.KIND_NODE_OPEN, C.KIND_NODE_CLOSE) and isinstance(payload, dict):
        return {f"bbx.node.{key}": _jsonable(payload[key]) for key in
                ("name", "kind", "parent", "outcome", "duration_us", "opened_ns") if key in payload}
    if kind in (C.KIND_RUN_START, C.KIND_RUN_END) and isinstance(payload, dict):
        return {f"bbx.run.{key}": _jsonable(payload[key]) for key in
                ("run_id", "argv", "pid", "stall_ms", "attached", "exit_code", "signaled", "source")
                if key in payload}
    if kind == C.KIND_GAP and isinstance(payload, dict):
        return {f"bbx.gap.{key}": _jsonable(payload[key])
                for key in ("from", "to", "reason", "segments", "prev") if key in payload}
    if kind == C.KIND_CHAIN and isinstance(payload, dict):
        # The chain's own values, as hex: the same text `chain.json` carries, so a reader can compare the two.
        return {f"bbx.chain.{key}": (payload[key].hex() if isinstance(payload[key], (bytes, bytearray))
                                     else _jsonable(payload[key]))
                for key in ("batch", "prev", "first_seq") if key in payload}
    if payload is None:
        return {"bbx.payload.bytes": len(frame.get("payload") or b"")}
    payload = _jsonable(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) <= _PAYLOAD_LIMIT:
        return {"bbx.payload": payload}
    return {"bbx.payload.bytes": len(encoded), "bbx.payload.sha256": hashlib.sha256(encoded).hexdigest(),
            "bbx.payload.truncated": True}


def line(frame: dict[str, Any], *, fallback_segment: int | None = None) -> dict[str, Any]:
    """One JSON object for one frame. Pure: the same frame gives the same object, in every process."""
    kind = int(frame.get("kind") or 0)
    segment = frame.get("segment", fallback_segment)
    return {
        "attributes": _attributes(frame),
        "kind": C.KIND_NAMES.get(kind, f"KIND_{kind}"),
        "kind_id": kind,
        "node_slot": int(frame.get("node_slot") or 0),
        "run_slot": int(frame.get("run_slot") or 0),
        "segment": int(segment) if segment is not None else None,
        "seq": int(frame.get("seq") or 0),
        "time_unix_nano": int(frame.get("wall_ns") or 0),
        "bbx_mono_nano": int(frame.get("mono_ns") or 0),
    }


def render(frames: list[dict[str, Any]]) -> bytes:
    """The whole file: one object per frame, LF-terminated, and a trailing newline so it is a text file."""
    out = bytearray()
    for frame in frames:
        out += json.dumps(line(frame), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        out += b"\n"
    return bytes(out)


def first_difference(stored: bytes, recomputed: bytes) -> dict[str, Any] | None:
    """The first line two renderings disagree on, or `None` when they agree — what a drift report names."""
    if stored == recomputed:
        return None
    left, right = stored.split(b"\n"), recomputed.split(b"\n")
    for index, (mine, theirs) in enumerate(zip(left, right, strict=False)):
        if mine != theirs:
            return {"line": index + 1, "stored": mine.decode("utf-8", "replace")[:400],
                    "recomputed": theirs.decode("utf-8", "replace")[:400]}
    return {"line": min(len(left), len(right)) + 1, "stored": None if len(left) > len(right)
            else left[len(left) - 1].decode("utf-8", "replace")[:400],
            "recomputed": None if len(right) > len(left) else right[len(right) - 1].decode("utf-8", "replace")[:400],
            "note": f"{len(left)} lines stored, {len(right)} recomputed"}


__all__ = ["SEMCONV", "SEMCONV_COMMIT", "first_difference", "line", "operation_of", "render", "system_of"]
