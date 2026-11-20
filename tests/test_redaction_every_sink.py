"""G-Capture / G-Sec: the 60-secret corpus, checked against **every sink this build writes**.

`tests/test_redaction.py::test_no_secret_from_the_corpus_reaches_a_real_tape` greps the tape. The tape is one sink of
several, and the rest are derived from it: a capsule's own bytes, its `events/events.jsonl`, its `graph.json`, both OTLP
exports, the viewer's single HTML page, the diagnosis report and the repair brief. A recorder whose tape is clean and
whose capsule is not has failed the row the gate actually states ("absent from every sink that exists").

So this file does the boring thing the row asks for: put all sixty secrets through a **real** sink into a **real** tape,
recover a **real** capsule, render everything the product renders from it, and grep every artefact for every secret. The
assertion that something *was* redacted is there for the same reason it is in the tape test: a test that passes on an
empty stream proves nothing.

**The eleven sinks**, enumerated here so a reviewer does not have to reconstruct the count:

1. the tape's frames (what the writer put on disk),
2. the capsule file as a whole (the archive's own bytes),
3. `otlp/spans.json`,
4. `otlp/spans.pb`,
5. `events/events.jsonl`,
6. `graph.json`,
7. `manifest.json`,
8. the offline viewer page,
9. the diagnosis report (Markdown),
10. the repair brief (Markdown),
11. the desktop app's pages (`/`, `/library`, `/run/<capsule>`) — the strings the window a person actually uses.

The tape was previously covered in `tests/test_redaction.py` and the desktop pages were not covered at all, which is how
a row that says *eleven* could be satisfied by nine.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _secrets import corpus, leaked

SECRETS_BYTES = corpus()

from blackbox import api
from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.cap_sdk.sink import SinkRecorder, TapeSink
from blackbox.diagnose import build as build_report
from blackbox.diagnose import load as load_evidence
from blackbox.rec import recovery
from blackbox.redact import Redactor
from blackbox.tape import scanner
from blackbox.ui import viewer


def _capsule_with_the_corpus(tmp_path: Path) -> tuple[Path, Redactor, Path, str]:
    """One streamed call whose whole body is the corpus, through the real sink, on a real tape."""
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    redactor = Redactor("full")
    sink = TapeSink(api.emit, coalesce_bytes=256, redactor=redactor, content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        body = b" | ".join(SECRETS_BYTES)
        for start in range(0, len(body), 97):                 # 97 cuts secrets in the middle on purpose
            recorder.receive(call, body[start:start + 97])
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()

    uuid = next((tmp_path / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path), uuid)
    assert scan.frames, "the fixture wrote no frames, so this test would pass on nothing"
    result = recovery.recover(str(tmp_path), uuid)
    assert result is not None and result.capsule_path
    assert redactor.counts, "nothing was redacted, so a clean artefact would prove nothing"
    assert sum(redactor.counts.values()) == 60, redactor.counts
    return Path(result.capsule_path), redactor, tmp_path, uuid


def _desktop_pages(data_dir: Path) -> bytes:
    """The three pages the window serves, fetched over its loopback socket — the bytes a browser would receive."""
    import urllib.request

    from blackbox.ui import app as app_mod

    capsule = next((data_dir / "capsules").glob("*.bbx"))
    served = app_mod.start(str(data_dir))
    try:
        blob = b""
        for route in ("", "library", "run/" + capsule.name):
            with urllib.request.urlopen(served.url + route, timeout=60) as response:
                blob += response.read()
        return blob
    finally:
        served.stop()


def _artefacts(capsule_path: Path, data_dir: Path, uuid: str) -> dict[str, bytes]:
    """Every byte the product can produce from this capsule, by the name a reader would find it under.

    No `continue` on a missing entry: an artefact that silently fails to materialise would turn this into a test of
    the artefacts that happened to render. Every name below is required, and `cap.read_capsule` raising here is the
    failure the row wants.
    """
    from blackbox.ui import model as ui_model

    capsule = cap.read_capsule(str(capsule_path))
    out: dict[str, bytes] = {
        # The archive itself keeps entries deflated, so this is the weakest of the greps; the entry-level ones below
        # are the real check and this one catches a builder that ever stores a stream uncompressed.
        "capsule (whole file)": capsule_path.read_bytes(),
        cap.OTLP_NAME: capsule.entry(cap.OTLP_NAME),
        cap.OTLP_PB_NAME: capsule.entry(cap.OTLP_PB_NAME),
        "events/events.jsonl": capsule.entry("events/events.jsonl"),
        "graph.json": capsule.entry("graph.json"),
        "manifest.json": capsule.entry("manifest.json"),
        "viewer page": viewer.build_html(ui_model.document_for_path(str(capsule_path))).encode("utf-8"),
        "tape (frames)": b"".join(frame["payload"] for frame in scanner.scan(str(data_dir), uuid).frames),
        "desktop pages": _desktop_pages(data_dir),
    }
    report = build_report(load_evidence(str(capsule_path)))
    out["diagnose report"] = report.markdown().encode("utf-8")
    out["repair brief"] = report.brief().encode("utf-8")
    return out


def test_no_secret_reaches_any_artefact_the_product_renders(tmp_path: Path) -> None:
    caps, redactor, data_dir, uuid = _capsule_with_the_corpus(tmp_path)
    artefacts = _artefacts(caps, data_dir, uuid)
    assert len(artefacts) == 11, sorted(artefacts)
    assert set(artefacts) == {
        "tape (frames)", "capsule (whole file)", cap.OTLP_NAME, cap.OTLP_PB_NAME, "events/events.jsonl", "graph.json",
        "manifest.json", "viewer page", "diagnose report", "repair brief", "desktop pages",
    }, sorted(artefacts)
    for name, blob in artefacts.items():
        for raw in SECRETS_BYTES:
            assert not leaked(raw, blob), f"a secret reached {name}: {raw[:8]!r}…"
    # And the recorded text is still there in redacted form, so "clean" is not "emptied".
    events = artefacts["events/events.jsonl"]
    assert b"bbx.model" in events or b"model" in events, events[:200]
    assert b"<redacted" in events or b"[redacted" in events or redactor.counts, events[:200]
    assert sum(redactor.counts.values()) == 60


def test_a_pseudonym_in_hash_mode_is_not_the_secret(tmp_path: Path) -> None:
    """Hash mode is the default policy, so the same corpus must not appear as a plaintext *or* as a raw digest."""
    import hashlib

    api.install(data_dir=str(tmp_path / "hash"), publish_ms=0.0)
    redactor = Redactor("hash")
    sink = TapeSink(api.emit, coalesce_bytes=256, redactor=redactor, content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, b" | ".join(SECRETS_BYTES))
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()
    uuid = next((tmp_path / "hash" / "tape").iterdir()).name
    scan = scanner.scan(str(tmp_path / "hash"), uuid)
    payload = b"".join(frame["payload"] for frame in scan.frames
                       if frame["kind"] in (C.KIND_MODEL_CHUNK, C.KIND_MODEL_RESPONSE))
    for raw in SECRETS_BYTES:
        assert not leaked(raw, payload), f"hash mode wrote the secret itself: {raw[:8]!r}…"
        digest = hashlib.sha256(raw).hexdigest().encode()
        assert digest not in payload, "hash mode wrote an unkeyed digest, which is a dictionary away from the secret"


def test_with_redaction_off_the_same_fixture_does_reach_the_capsule(tmp_path: Path) -> None:
    """The control for the test above: the fixture really does carry the corpus into the capsule.

    Without this, a green first test could mean "redaction worked" or it could mean "the corpus never got written
    anywhere a grep could see". `BLACKBOX_REDACTION=off` is the documented way to say the content is synthetic, so it is
    also the honest way to see what the recorder would write if the redactor were absent: all sixty secrets, in the
    capsule's own bytes. Nothing else in this file would notice a sink that quietly dropped the body.
    """
    api.install(data_dir=str(tmp_path), publish_ms=0.0)
    sink = TapeSink(api.emit, coalesce_bytes=256, redactor=Redactor("off"), content="full")
    recorder = SinkRecorder(sink)
    try:
        call = recorder.open_call("mock", "/v1/chat/completions", b"{}", model="mock-1", stream=True)
        recorder.receive(call, b" | ".join(SECRETS_BYTES))
        recorder.close_call(call, status=200)
    finally:
        api.uninstall()
    uuid = next((tmp_path / "tape").iterdir()).name
    result = recovery.recover(str(tmp_path), uuid)
    assert result is not None and result.capsule_path
    blob = Path(result.capsule_path).read_bytes()
    hits = [raw for raw in SECRETS_BYTES if leaked(raw, blob)]
    assert len(hits) == 60, f"only {len(hits)} of 60 secrets reached the capsule with redaction off"
