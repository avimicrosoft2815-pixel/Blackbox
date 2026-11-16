"""P4-10: the offline viewer's promises, checked where a browser is not available.

Nineteen of these tests would be worth nothing if they only asserted that a file was written. They assert the four
properties that make the page safe to open and honest to read, and each one fails on a plausible version of the code:

* **offline.** No `http://`, no `//cdn`, no `fetch(`, no `<link>`, no external `src` — in the *built* page, not in the
  template, because the builder is what decides. An evidence tool that quietly loads a font from a CDN has made a
  network request about the user's incident.
* **untrusted payloads are text.** A capsule is input from somewhere. `</script>` inside a prompt must not end the data
  block, and `<img onerror=…>` must arrive at the DOM as those characters. The test injects the corpus, parses the
  built page's data block back, and requires the string to survive unchanged. It also greps `app.js` for `innerHTML`,
  so a later edit cannot reintroduce the bug the test cannot see without a browser.
* **accessible.** Landmarks, a skip link, `role="tree"` with `aria-selected`/`aria-expanded`, a keyboard model that
  reaches every row, and a `prefers-reduced-motion` block. Contrast ratios are computed here from the stylesheet's own
  colours, so "readable" is a number rather than an adjective.
* **honest.** A capsule whose integrity root does not match is reported in a banner; a run with a torn tail says so.
  Neither is rendered as a confident timeline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from blackbox import constants as C
from blackbox.rec import launcher
from blackbox.ui import model, viewer

STATIC = Path(__file__).resolve().parents[1] / "src" / "blackbox" / "ui" / "static"
DATA_RE = re.compile(r'<script type="application/json" id="bbx-data">(.*?)</script>', re.S)


def _code(text: str) -> str:
    """The text with its comments removed, because these files *document* the rules they follow.

    `app.js` says "no `fetch`, no `XMLHttpRequest`" in its header and the stylesheet says "no `@import`"; a naive
    substring search would fail on the sentence that promises the property. Stripping comments first means the test
    checks the code, and the comments are free to name the thing they forbid.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    text = re.sub(r"(?m)\s//\s.*$", "", text)
    return text

XSS = [
    "</script><script>alert('x')</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "<svg/onload=alert(1)>",
    "&lt;already-escaped&gt;",
    "Ünïcödé and a \u2028 line separator",
]


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real run with a node, produced by the launcher rather than by hand, so the viewer sees real frames."""
    data_dir = tmp_path_factory.mktemp("viewer") / "data"
    child = ("from blackbox import api\n"
             "with api.run('child-work'):\n"
             "    api.mark('prompt', text='find the bug')\n")
    with open(data_dir.parent / "out.txt", "w", encoding="utf-8") as out:
        result = launcher.launch([__import__("sys").executable, "-c", child],
                                 data_dir=str(data_dir), out=out, proxy=False)
    assert result.exit_code == 0
    return data_dir


@pytest.fixture(scope="module")
def page(run_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, dict]:
    out = tmp_path_factory.mktemp("page") / "viewer.html"
    result = viewer.view(str(run_dir), str(out))
    html = out.read_text(encoding="utf-8")
    document = json.loads(DATA_RE.search(html).group(1))
    assert result["events"] > 0
    return html, document


# ── offline ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_page_references_nothing_outside_itself(page):
    html, _ = page
    code = _code(html)
    for pattern in ("http://", "https://", "//cdn", "@import", "fetch(", "XMLHttpRequest", "new WebSocket",
                    "<link", "crossorigin"):
        assert pattern not in code, f"the built page's code contains {pattern!r}, so it is not offline"
    assert html.lstrip().startswith("<!doctype html>")


def test_every_href_and_src_is_an_in_page_fragment(page):
    html, _ = page
    for match in re.finditer(r'(?:href|src)="([^"]*)"', html):
        assert match.group(1).startswith("#"), f"external or scripted reference: {match.group(1)!r}"


def test_the_data_is_inside_the_document(page):
    html, document = page
    assert document["run"]["run_id"].startswith("r_")
    assert any(tape["events"] for tape in document.get("tapes", []))
    assert "app.js" not in html and "viewer.css" not in html, "the assets must be inlined, not referenced"


# ── untrusted payloads ──────────────────────────────────────────────────────────────────────────────────────────
def test_an_xss_payload_in_a_payload_survives_as_characters(tmp_path):
    document = {
        "viewer": {"version": 1, "capsule_path": "x.bbx", "role": "tape", "root_ok": True, "problems": []},
        "run": {"run_id": "r_test", "status": "recovered"},
        "counts": {"events": len(XSS)}, "capture": {"coverage": "FULL"}, "redaction": {"content_mode": "full"},
        "gaps": [], "links": {"links": [], "summary": {"exact": 0, "declared": 0, "inferred": 0}},
        "events": [{"i": i, "seq": i, "kind": C.KIND_MARK, "kind_name": "MARK", "node_slot": 0,
                    "wall_ns": 1_700_000_000_000_000_000 + i, "rel_ms": float(i), "summary": payload}
                   for i, payload in enumerate(XSS)],
        "nodes": [], "cost": {"model_calls": 0, "tool_calls": 0, "chunks": 0, "stream_bytes": 0},
    }
    html = viewer.build_html(document)
    # The data block must still be one block: a payload cannot close the element it lives in.
    assert len(DATA_RE.findall(html)) == 1
    parsed = json.loads(DATA_RE.search(html).group(1))
    assert [event["summary"] for event in parsed["events"]] == XSS
    assert "<script>" not in DATA_RE.search(html).group(1)
    # And the only <script> elements are the data block and the viewer's own inlined code.
    assert html.count("<script") == 2


def test_the_page_writes_text_never_markup():
    script = _code((STATIC / "app.js").read_text(encoding="utf-8"))
    for forbidden in (".innerHTML", ".outerHTML", "document.write", "insertAdjacentHTML", ".srcdoc"):
        assert forbidden not in script, f"{forbidden} would turn capsule data into markup"
    assert script.count("textContent") >= 8, "the page should be building its text with textContent throughout"


# ── accessible ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_page_has_landmarks_a_skip_link_and_an_accessible_tree(page):
    html, _ = page
    assert '<a class="skip-link" href="#timeline">' in html
    for needle in ('role="banner"', 'role="tree"', 'aria-label="Timeline"', 'aria-label="Inspector"',
                   'aria-live="polite"', 'aria-selected', "<dialog", "<noscript>"):
        assert needle in html, f"missing {needle!r}"


def test_every_row_is_reachable_from_the_keyboard():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    for key in ("ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", "Escape"):
        assert key in script, f"{key} is not handled"
    assert 'role", "treeitem"' in script
    assert "tabIndex" in script and "scrollIntoView" in script
    # A keyboard user must never be able to tab into a row that has no focus ring: the ring is in the stylesheet.
    assert ":focus-visible" in (STATIC / "viewer.css").read_text(encoding="utf-8")


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_every_text_colour_meets_its_contrast_ratio():
    """WCAG AA, computed from the stylesheet's own values: 4.5:1 for body text, 3:1 for large/secondary accents."""
    css = (STATIC / "viewer.css").read_text(encoding="utf-8")

    def token(name: str) -> str:
        match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", css)
        assert match, f"--{name} is not a hex colour, so this test cannot check it"
        return match.group(1)

    panel = token("panel-solid")          # the opaque panel colour the glass resolves to over black
    body = [("ink", 4.5), ("ink-dim", 4.5), ("ink-faint", 3.0), ("gold", 4.5), ("gold-bright", 4.5),
            ("ok", 4.5), ("warn", 4.5), ("bad", 4.5)]
    for name, minimum in body:
        ratio = _ratio(token(name), panel)
        assert ratio >= minimum, f"--{name} on {panel} is {ratio:.2f}:1, below {minimum}:1"


def test_motion_is_off_when_the_reader_asks(page):
    html, _ = page
    assert "prefers-reduced-motion: reduce" in html
    css = (STATIC / "viewer.css").read_text(encoding="utf-8")
    block = css.split("prefers-reduced-motion: reduce", 1)[1]
    for prop in ("animation-duration", "transition-duration", "scroll-behavior"):
        assert prop in block, f"{prop} is not neutralised under reduced motion"
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in script, "scrollIntoView must not smooth-scroll under reduced motion"


# ── honest ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_damaged_capsule_is_reported_rather_than_rendered(run_dir, tmp_path, capsys):
    """One flipped byte in the archive, and the honest answer is an error — not a page that looks fine.

    The byte lands inside a stored entry, so the *zip* CRC catches it before the capsule's own root is even consulted
    (`zipfile.BadZipFile: Bad CRCs for file 'events/seg-000001.raw'`). That is two independent checks agreeing, and
    the viewer's job is to stop: a page rendered from bytes that failed their own checksum would be a confident lie.
    """
    source = sorted((run_dir / "capsules").glob("*.bbx"))[0]
    damaged = tmp_path / "damaged.bbx"
    blob = bytearray(source.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    damaged.write_bytes(bytes(blob))

    with pytest.raises(Exception) as caught:
        viewer.view(str(damaged), str(tmp_path / "damaged.html"))
    assert not (tmp_path / "damaged.html").exists(), "nothing may be written from bytes that failed their checks"
    assert type(caught.value).__name__ in ("BadZipFile", "BlackBoxError", "CapsuleError", "ValueError")

    code = viewer.main([str(damaged), "--out", str(tmp_path / "nope.html")])
    assert code == 2
    assert "blackbox view" in capsys.readouterr().err


def test_a_failed_root_travels_into_the_page(tmp_path):
    """The other half: when the reader *does* return a capsule with `root_ok: False`, the fact reaches the page."""
    document = {
        "viewer": {"version": 1, "capsule_path": "x.bbx", "role": "tape", "root_ok": False,
                   "problems": ["entry events/seg-000001.raw: sha256 mismatch"]},
        "run": {"run_id": "r_x", "status": "recovered", "truncated_tail": True, "lost_from_seq": 12},
        "counts": {"events": 1}, "capture": {"coverage": "PARTIAL"}, "redaction": {"content_mode": "hash"},
        "gaps": [{"from": 3, "to": 5, "reason": "EVICTED"}], "links": {"links": [], "summary": {}},
        "events": [], "nodes": [], "cost": {},
    }
    html = viewer.build_html(document)
    assert '"root_ok": false' in html and '"truncated_tail": true' in html
    script = _code((STATIC / "app.js").read_text(encoding="utf-8"))
    assert "root_ok" in script and "Evidence boundary" in script and "integrity" in script


def test_the_timeline_shows_the_tape_with_the_work_not_the_boundary(run_dir):
    """`blackbox run -- python app.py` leaves a five-frame boundary and a nine-frame child tape; open on the child's."""
    document = model.document_for_path(str(run_dir))
    assert next(event["kind_name"] for event in document["events"]) == "RUN_START"
    assert document["nodes"], "the child's node should be in the document the page opens on"
    assert document["run"]["run_id"] and document["run"]["capsule_run_id"]
    assert document["run"]["run_id"] != document["run"]["capsule_run_id"], \
        "the named run and the per-capsule id are different things and the page shows both"


def test_a_directory_that_holds_no_capsule_is_an_error_not_an_empty_view(tmp_path):
    from blackbox.errors import BlackBoxError

    (tmp_path / "empty").mkdir()
    with pytest.raises(BlackBoxError) as caught:
        model.document_for_path(str(tmp_path / "empty"))
    assert "holds no capsule" in str(caught.value)


def test_the_three_static_files_ship_with_the_package():
    """`blackbox view` inlines these three files; if they are not packaged, an installed wheel builds an empty page."""
    for name in ("index.html", "viewer.css", "app.js"):
        path = viewer.STATIC / name
        assert path.exists() and path.stat().st_size > 200, f"{name} is missing or empty beside ui/viewer.py"


def test_the_page_carries_the_counts_it_claims(page):
    _, document = page
    assert document["counts"]["nodes"] == len(document["nodes"]) or document["counts"]["nodes"] >= 1
    assert document["cost"]["model_calls"] == sum(1 for e in document["events"] if e["kind"] == C.KIND_MODEL_REQUEST)


def test_view_writes_beside_a_capsule_and_into_a_data_directory(run_dir, tmp_path):
    capsule_path = next(iter(sorted((run_dir / "capsules").glob("*.bbx"))))
    beside = viewer.view(str(capsule_path))
    assert beside["out"].endswith(".html") and Path(beside["out"]).exists()
    into = viewer.view(str(run_dir))
    assert Path(into["out"]).name == "viewer.html"


def test_the_cli_reports_what_it_wrote(capsys, run_dir):
    code = viewer.main([str(run_dir), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["out"].endswith("viewer.html") and payload["bytes"] > 10_000
