"""What a capsule can say about itself, and what it cannot (P4-16).

`blackbox verify` is one command with three ways to name its target — a capsule file, a run id, or a writer uuid — and
five checks behind it. The interesting part is not the list; it is that **two of the five are not checkable from the
capsule's own bytes**, and the report says so instead of printing a green line over them:

| check | recomputed from | verdict |
|---|---|---|
| `integrity` | every entry's bytes against `INTEGRITY.sha256` and its root | verified |
| `chain_root` | the `CHAIN` frames the capsule carries | verified |
| `derived_parts` | `graph.json`, `events/events.jsonl`, `otlp/spans.json` re-rendered from the segments | verified (`BBX-E412` on drift) |
| `capture_seam` | the `capture_seam` values on the capsule's own `MODEL_REQUEST` frames | verified |
| `capture_vantage` | **nothing** — a capsule carries no `meta` page | `declared` — printed as unverified, never as a pass |

The last row is the one worth being stubborn about. The vantage is a property of the *tape* — which tap wrote it — and
the capsule deliberately carries only what it can prove about its own bytes: segments, graph, exports, `links.json`,
`chain.json`. A verifier that printed `capture_vantage: ok` because the number in the manifest looked plausible would be
reporting a check it never ran, which is the failure mode D-056 calls out in the schema snapshot and this file calls out
here. The value is still declared and printed, because a reader who *has* the tape can compare it; the capsule alone
cannot.

**Pairs, not a pile.** Every check reports `declared` and `recomputed` side by side, so a failure shows both numbers rather
than a verdict. `ok` is `True`, `False`, or `None` (not checkable) — and only `False` fails the command, so an old capsule
whose manifest predates `capture_seam` is *absent*, not broken. Exit codes are the ones the plan fixes: `0` verified, `9`
something failed, `2` the target cannot be read at all.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from blackbox import constants as C
from blackbox.cap import derived
from blackbox.cap import format as cap
from blackbox.cap import integrity as integrity_mod
from blackbox.errors import bbx

#: The exit code the plan fixes for "a check failed" (P4-16). Distinct from 2 (bad input) on purpose: a script must be
#: able to tell "this capsule is wrong" from "you pointed me at a file I cannot open".
EXIT_DRIFT = 9

#: The frame kinds that carry `capture_seam`. The SDK's sink writes it on the request, not on the response, and the
#: capsule can therefore recompute the set without a decoder.
_SEAM_KIND = C.KIND_MODEL_REQUEST


def capsule_id_for(run_id: str, writer_uuid_hex: str) -> str:
    """The capsule id recovery uses, in one place: `c_` + blake2b(run|writer)[:16].

    `blackbox verify --writer <uuid>` needs this to answer "which of these capsules came from that tape?" without a
    writer field in the manifest — the id is derived from the writer and the run, so it can be *recomputed* per capsule
    and compared, which is a check rather than a lookup.
    """
    data = b"|".join(part.encode() for part in (str(run_id), str(writer_uuid_hex)))
    return "c_" + hashlib.blake2b(data).hexdigest()[:16]


def _payloads(frames: list[dict[str, Any]], kind: int) -> list[dict[str, Any]]:
    from blackbox.cap import export_jsonl

    out: list[dict[str, Any]] = []
    for frame in frames:
        if int(frame.get("kind") or 0) != kind:
            continue
        payload = export_jsonl._payload_of(frame)
        if isinstance(payload, dict):
            out.append(payload)
    return out


def chain_root_of(frames: list[dict[str, Any]]) -> str | None:
    """The last `CHAIN` frame's batch value, or `None` when the capsule carries no chain frame.

    This is the value `chain.json` calls `chain_root`: the walk's end. Recomputing it from the frames rather than reading
    `chain.json` is the point — `chain.json` is an entry like any other, and a verifier that trusted it would be checking
    a claim with the claim.
    """
    value: str | None = None
    for frame in frames:
        if int(frame.get("kind") or 0) != C.KIND_CHAIN:
            continue
        try:
            from blackbox.tape import chain as chain_mod

            body = chain_mod.decode_payload(frame["payload"])
        except Exception:
            continue
        value = bytes(body["batch"]).hex()
    return value


def seams_of(frames: list[dict[str, Any]]) -> list[str]:
    """The distinct `capture_seam` values the capsule's own model requests carry, sorted."""
    return sorted({str(item["capture_seam"]) for item in _payloads(frames, _SEAM_KIND)
                   if isinstance(item.get("capture_seam"), str) and item["capture_seam"]})


def links_summary_of(document: bytes) -> dict[str, Any]:
    """Counts over `links.json`, recomputed from its bytes — the same shape recovery declares in the manifest."""
    if not document.strip():
        return {"links": 0, "call_links": 0, "resolved": 0, "unresolved": 0}
    try:
        parsed = json.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"links": 0, "call_links": 0, "resolved": 0, "unresolved": 0, "unreadable": True}
    if not isinstance(parsed, dict):
        return {"links": 0, "call_links": 0, "resolved": 0, "unresolved": 0, "unreadable": True}
    raw_links, raw_calls = parsed.get("links"), parsed.get("call_links")
    links: list[Any] = raw_links if isinstance(raw_links, list) else []
    calls: list[Any] = raw_calls if isinstance(raw_calls, list) else []

    def resolved(records: list[Any]) -> int:
        return sum(1 for record in records
                   if isinstance(record, dict) and str(record.get("resolution", record.get("class", ""))).lower()
                   in ("exact", "resolved", "same_run"))

    return {"links": len(links), "call_links": len(calls), "resolved": resolved(links) + resolved(calls),
            "unresolved": len(links) + len(calls) - (resolved(links) + resolved(calls))}


def _check(name: str, *, declared: Any, recomputed: Any, ok: bool | None, note: str | None = None,
           problems: list[str] | None = None) -> dict[str, Any]:
    check: dict[str, Any] = {"check": name, "declared": declared, "recomputed": recomputed, "ok": ok,
                             "checkable": ok is not None}
    if note:
        check["note"] = note
    if problems:
        check["problems"] = problems
    return check


def verify(capsule: cap.Capsule, *, frames: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run every check the capsule can answer. Returns pairs, not verdicts, and never writes anything."""
    manifest = capsule.manifest
    frames = derived.frames_of(capsule) if frames is None else frames
    checks: list[dict[str, Any]] = []

    # P4-03: the integrity check names the entry that changed, because "the capsule was modified" is not something a
    # reader can act on. The report is recomputed here from the entries themselves (not read back from `read_capsule`),
    # so the check is a comparison and not a repetition of what the reader already decided.
    declared_root = (manifest.get("integrity") or {}).get("root")
    integrity_report = integrity_mod.verify(capsule.entries, capsule.entries.get(cap.INTEGRITY_NAME, b""),
                                            declared_root)
    checks.append(_check("integrity", declared=declared_root,
                         recomputed={"matched": integrity_report["matched"],
                                     "mismatched": [item["name"] for item in integrity_report["mismatched"]],
                                     "missing": integrity_report["missing"],
                                     "unlisted": integrity_report["unlisted"],
                                     "root_ok": integrity_report["root"]["ok"]},
                         ok=integrity_report["ok"],
                         note=None if integrity_report["ok"] else "the capsule's own entries are not the ones it sealed",
                         problems=integrity_report["problems"]))

    declared_chain = manifest.get("chain_root")
    fresh_chain = chain_root_of(frames)
    if declared_chain is None and fresh_chain is None:
        checks.append(_check("chain_root", declared=None, recomputed=None, ok=None,
                             note="this capsule declares no chain root and carries no CHAIN frame: nothing to compare"))
    else:
        checks.append(_check("chain_root", declared=declared_chain, recomputed=fresh_chain,
                             ok=None if declared_chain is None else declared_chain == fresh_chain,
                             note="declared in the manifest; recomputed from the capsule's own CHAIN frames"
                             if declared_chain is not None else
                             "the capsule carries a chain but its manifest declares no root: a reader cannot compare"))

    parts = derived.verify(capsule)
    checks.append(_check("derived_parts", declared=manifest.get("derived_parts") or [],
                         recomputed={"drift": parts["drift"], "absent": parts["absent"],
                                     "unknown": parts.get("unknown", [])},
                         ok=bool(parts["ok"]),
                         note="each declared part re-rendered from the segments; drift is BBX-E412"))

    declared_seams = manifest.get("capture_seam")
    fresh_seams = seams_of(frames)
    if declared_seams is None:
        checks.append(_check("capture_seam", declared=None, recomputed=fresh_seams, ok=None,
                             note="this capsule predates the field: absent, not wrong (D-056 ships it from P4-16 on)"))
    else:
        checks.append(_check("capture_seam", declared=sorted(declared_seams), recomputed=fresh_seams,
                             ok=sorted(declared_seams) == fresh_seams))

    checks.append(_check("capture_vantage", declared=manifest.get("capture_vantage"), recomputed=None, ok=None,
                         note="a capsule carries no meta page: the vantage is declared and cannot be recomputed here, "
                              "so it is printed as unverified rather than counted as a pass"))

    declared_links = manifest.get("links_summary")
    fresh_links = links_summary_of(capsule.entries.get(cap.LINKS_NAME, b""))
    if declared_links is None:
        checks.append(_check("links_summary", declared=None, recomputed=fresh_links, ok=None,
                             note="this capsule predates the field"))
    else:
        checks.append(_check("links_summary", declared=declared_links, recomputed=fresh_links,
                             ok=declared_links == fresh_links,
                             note="recomputed from links.json inside the capsule"))

    failed = [check["check"] for check in checks if check["ok"] is False]
    uncheckable = [check["check"] for check in checks if check["ok"] is None]
    return {"ok": not failed, "checks": checks, "failed": failed, "uncheckable": uncheckable}


#: The manifest schema this build validates on write and on read (P4-02), written as data rather than as prose because a
#: schema nobody executes is a paragraph. `(spec, required)` pairs; `spec` is a type name, or a nested dict of the same
#: shape. `any` accepts anything (a signature, a future value we do not police), `*_or_null` accepts the type or `None`.
#:
#: **Required means every capsule this format has ever produced carries it.** Keys added later — `derived_parts`
#: (P4-13), `chain_root`/`capture_vantage`/`capture_seam`/`links_summary` (P4-16) — are optional, because the
#: compatibility rule points both ways: a new build must not call an older capsule corrupt (D-053's rule, D-057's rule,
#: and this one). A capsule from before a field existed verifies as *absent*, never as invalid.
SCHEMA: dict[str, tuple[object, bool]] = {
    "format": ("str", True),
    "format_version": ("int", True),
    "capsule_id": ("str", True),
    "created_ns": ("int", True),
    "tool": ({"name": ("str", True), "version": ("str", True), "git": ("str", False)}, True),
    "run": ({"run_id": ("str", True), "status": ("str", True), "name": ("str", False),
             "status_cause": ("str_or_null", False), "durability_class": ("str", False),
             "completeness": ("str", False), "truncated_tail": ("bool", False),
             "lost_from_seq": ("int_or_null", False), "started_ns": ("int", False), "ended_ns": ("int", False)},
            True),
    "capture": ({"coverage": ("str", True), "providers": ("list", False), "sdk_matrix": ("str", False),
                 "unsupported_ops": ("int", False)}, True),
    "redaction": ({"profile_id": ("str", True), "content_mode": ("str", True), "masks": ("int", False),
                   "failures": ("int", False), "key_included": ("bool", False)}, True),
    "gaps": ("list", True),
    "counts": ({"events": ("int", True), "nodes": ("int", True), "bytes": ("int", False),
                "segments": ("int", False), "nodes_interrupted": ("int", False),
                "model_calls": ("int", False), "tool_calls": ("int", False)}, True),
    "integrity": ({"root": ("str", True), "alg": ("str", False), "entries": ("int", False)}, True),
    "signature": ("any", False),
    "min_reader": ("str", False),
    # Added after the first capsules were written; optional for exactly that reason.
    "derived_parts": ("list_of_parts", False),
    "chain_root": ("str_or_null", False),
    "capture_vantage": ("int_or_null", False),
    "capture_seam": ("list_of_str", False),
    "links_summary": ("dict_of_int", False),
}

#: The keys this build knows. Anything else is *tolerated* — an unknown key is a newer writer's field, not a lie — and
#: `validate` returns them so a caller can mention them without failing.
KNOWN_KEYS = frozenset(SCHEMA)


def _type_ok(spec: str, value: object) -> bool:
    if spec == "any":
        return True
    if spec == "str":
        return isinstance(value, str)
    if spec == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "bool":
        return isinstance(value, bool)
    if spec == "list":
        return isinstance(value, list)
    if spec == "str_or_null":
        return value is None or isinstance(value, str)
    if spec == "int_or_null":
        return (value is None) or (isinstance(value, int) and not isinstance(value, bool))
    if spec == "list_of_str":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if spec == "dict_of_int":
        return (isinstance(value, dict)
                and all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                        for k, v in value.items()))
    if spec == "list_of_parts":
        return isinstance(value, list) and all(
            isinstance(part, dict) and isinstance(part.get("name"), str) and isinstance(part.get("sha256"), str)
            for part in value)
    raise AssertionError(f"schema spec {spec!r} is not one this validator implements")


def _walk(value: dict[str, Any], schema: dict[str, tuple[object, bool]], where: str, prefix: str = "") -> None:
    for key, (spec, required) in schema.items():
        path = prefix + key
        if key not in value:
            if required:
                raise bbx("BBX-E407", f"{where}: manifest is missing required key {path!r}")
            continue
        item = value[key]
        if isinstance(spec, dict):
            if not isinstance(item, dict):
                raise bbx("BBX-E407", f"{where}: manifest {path!r} must be an object, found {type(item).__name__}")
            _walk(item, spec, where, path + ".")
            continue
        if not _type_ok(str(spec), item):
            raise bbx("BBX-E407", f"{where}: manifest {path!r} must be {spec}, found {type(item).__name__}")


def validate(manifest: dict[str, Any], *, where: str = "manifest") -> list[str]:
    """Check a manifest against `SCHEMA`; return the unknown top-level keys (tolerated, not ignored silently).

    Raises `BBX-E407` for a missing required key or a wrong type. Unknown keys are **not** an error: a capsule written
    by a newer build must open in an older one (its extra fields are that build's business), and the reverse — refusing
    a capsule because it carries a field this build has not learned — would make the format's forward compatibility a
    claim instead of a property.
    """
    if not isinstance(manifest, dict):                                    # pragma: no cover - read_capsule parses JSON
        raise bbx("BBX-E407", f"{where}: manifest is not an object")
    _walk(manifest, SCHEMA, where)
    return sorted(set(manifest) - KNOWN_KEYS)


__all__ = ["EXIT_DRIFT", "KNOWN_KEYS", "SCHEMA", "capsule_id_for", "chain_root_of", "links_summary_of", "seams_of",
           "validate", "verify"]
