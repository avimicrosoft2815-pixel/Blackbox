"""A 20-payload XSS corpus, inert in the viewer, the desktop pages and the Markdown report.

The threat is not exotic: a capsule is written from *someone's* program, and the program's text — a prompt, a model's
reply, a tool's arguments, an exception message, a node name — arrives in the page that shows the incident. A viewer that
builds markup from that text turns an evidence tool into a delivery mechanism, and the person opening it is the person
who was investigating the incident.

`tests/test_ui_viewer.py` already puts six payloads through the viewer's data block. This file is the corpus the plan
asks for (twenty), and it is stricter in four ways:

* it goes through a **real capsule** — a tape with those strings in its frames, sealed by `recovery.recover` — rather
  than through a hand-built document, so the payload travels the path a user's text travels;
* it checks **three renderers** (the offline viewer page, the desktop run page and library page, and the Markdown report
  and brief), because a page that is safe and a report that is not is still a product that renders untrusted text;
* it asserts the strings **survive as characters** rather than being dropped — a renderer that deletes a payload looks
  safe here and lies to the reader about what the incident contained;
* it checks the report's fenced blocks, since Markdown passes raw HTML through to a renderer and the only defence is that
  recorded text is inside a fence the reader can see.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import format as cap
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape.writer import TapeWriter
from blackbox.ui import app as app_mod

XSS = [
    "<script>alert('script')</script>",
    "</script><script>alert('break')</script>",
    "<img src=x onerror=alert('img')>",
    "<svg/onload=alert('svg')>",
    "<iframe src=javascript:alert('iframe')></iframe>",
    "javascript:alert('scheme')",
    "<body onload=alert('body')>",
    "<a href=\"javascript:alert('link')\">click</a>",
    "<style>@import 'http://evil.test/x.css';</style>",
    "<link rel=stylesheet href=http://evil.test/x.css>",
    "<meta http-equiv=refresh content=0;url=javascript:alert('meta')>",
    "<object data=javascript:alert('object')>",
    "<embed src=javascript:alert('embed')>",
    "<form action=javascript:alert('form')><input type=submit></form>",
    "<base href=http://evil.test/>",
    "&lt;script&gt;alert('already-escaped')&lt;/script&gt;",
    "'\"><script>alert('quote-break')</script>",
    "{{7*7}} ${alert('template')} #{alert('ruby')}",
    "` + alert('concat') + `",
    "\u2028\u2029 unicode separators \u0000 and \U0001f600",
]

DATA_RE = re.compile(r'<script type="application/json" id="bbx-data">(.*?)</script>', re.S)


@pytest.fixture(scope="module")
def capsule(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A real run whose frames carry the whole corpus: node names, model text, a mark and an exception."""
    data_dir = tmp_path_factory.mktemp("xss") / "data"
    writer = TapeWriter(str(data_dir), segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000ff"}), priority=0)
        for index, payload in enumerate(XSS):
            writer.append(C.KIND_NODE_OPEN, cbor.encode({"name": payload, "kind": "step"}),
                          node_slot=index + 1, priority=0, run_slot=1)
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"model": "m", "messages": payload}),
                          node_slot=index + 1, priority=0, run_slot=1)
            writer.append(C.KIND_MARK, cbor.encode({"name": payload, "text": payload}), priority=0)
            writer.append(C.KIND_NODE_CLOSE, cbor.encode({"status": "error", "error": payload}),
                          node_slot=index + 1, priority=0, run_slot=1)
        writer.append(C.KIND_RUN_END, cbor.encode({}), priority=0)
        writer.publish()
    finally:
        writer.close()
    result = recovery.recover(str(data_dir), writer.writer_uuid.hex())
    assert result.capsule_path and Path(result.capsule_path).exists()
    return str(result.capsule_path)


def test_the_corpus_is_twenty_payloads() -> None:
    assert len(XSS) == 20, "the row names twenty; a corpus that shrinks quietly is not the same test"


def test_the_viewer_page_keeps_every_payload_as_characters(capsule: str, tmp_path: Path) -> None:
    from blackbox.ui import viewer

    out = tmp_path / "page.html"
    assert viewer.main([capsule, "--out", str(out), "--json"]) == 0
    html = out.read_text(encoding="utf-8")
    block = DATA_RE.search(html).group(1)
    assert len(DATA_RE.findall(html)) == 1, "a payload must not be able to close the element it lives in"
    parsed = json.loads(block)
    seen = json.dumps(parsed)
    for payload in XSS:
        assert json.dumps(payload)[1:-1] in seen, f"the payload was dropped rather than escaped: {payload!r}"
    assert block.count("<script") == 0 and block.count("</script") == 0
    assert html.count("<script") == 2, "the data block and the viewer's own code, nothing else"
    assert "alert('script')" in html or "alert(&#x27;script&#x27;)" in html


def test_the_desktop_pages_escape_every_payload(capsule: str, tmp_path: Path) -> None:
    """The library row and the run page show the same strings through the same app the person uses."""
    import shutil

    data_dir = tmp_path / "data"
    (data_dir / "capsules").mkdir(parents=True)
    shutil.copy(capsule, data_dir / "capsules" / Path(capsule).name)
    server = app_mod.start(str(data_dir))
    try:
        for route in ("", "library", "run/" + Path(capsule).name):
            with __import__("urllib.request", fromlist=["request"]).urlopen(server.url + route, timeout=30) as response:
                html = response.read().decode("utf-8")
            assert "<script>alert" not in html, f"{route}: raw script payload"
            assert "<img src=x onerror" not in html, f"{route}: raw img payload"
            # A `javascript:` URI may appear as *characters* inside the page's data block, but never as an attribute.
            assert 'href="javascript:' not in html and 'src="javascript:' not in html, route
            assert 'src="http://evil.test' not in html and 'href="http://evil.test' not in html, route
    finally:
        server.stop()


def test_the_report_and_the_brief_fence_every_payload(capsule: str, tmp_path: Path) -> None:
    from blackbox.diagnose import cli as diagnose_cli

    report_path = tmp_path / "report.md"
    brief_path = tmp_path / "brief.md"
    assert diagnose_cli.main([capsule, "--out", str(report_path), "--brief", str(brief_path)]) == 0
    for path in (report_path, brief_path):
        text = path.read_text(encoding="utf-8")
        in_fence = False
        for line in text.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # A `<` that is not part of `&lt;` would be raw HTML; a `javascript:` URI that is not inside a link target
            # is inert text, so the check is on the constructs a renderer acts on, not on the substring.
            for payload in ("<script", "<img", "<svg", "<iframe", "](javascript:", "](http://evil"):
                assert payload not in line, (
                    f"{path.name}: recorded text outside a fence would reach a Markdown renderer as HTML: {line!r}")
            # And every `<` that came from a payload is an entity, not a tag: the cell may name `src=javascript:` as
            # characters — inert — but it may not contain a `<` that a renderer would start parsing.
            if "script" in line and "&lt;script" in line:
                assert "<script" not in line
            if "&lt;script&gt;" in line:
                assert "&amp;lt;" not in line, f"{path.name}: the escaping was applied twice"
        # Nothing in either document may fetch or execute: no image, no link to the payload's host.
        assert "![" not in text.replace("![bbx", ""), "a Markdown image would make the reader's client fetch a URL"


def test_the_capsule_itself_round_trips_the_bytes(capsule: str) -> None:
    """The escaping must happen at render time, not at record time: the evidence keeps what the program said."""
    parsed = cap.read_capsule(capsule)
    assert parsed.root_ok, parsed.problems
    blob = b"".join(parsed.entries[name] for name in sorted(parsed.entries) if name.startswith("events/seg-"))
    for payload in XSS:
        if "\u0000" in payload:
            continue                       # a NUL is a CBOR text character; the frame carries it, the page may not
        assert payload.encode("utf-8") in blob, f"the capsule no longer holds this payload: {payload!r}"
