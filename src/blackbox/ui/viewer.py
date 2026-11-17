"""`blackbox view` — one HTML file, opened from disk, that renders a capsule without a single network request.

Why a *file* and not a server: the directive's constraints are "offline" and "no egress", and a static document is the
only shape that satisfies both by construction rather than by configuration. There is no port to bind, no token to
leak, no CORS policy to get wrong, and the page still works in ten years because the data is inside it. The trade is
that the file is as big as its capsule's summary (~40 KB for a small run), which is the right trade for evidence.

What the builder guarantees, and `tests/test_ui_viewer.py` checks:

* **no external reference of any kind.** No scheme-qualified URL, no protocol-relative host, no stylesheet import, no
  `fetch`/`XMLHttpRequest` — none may appear in the built page (`test_no_url_literals` enforces the same rule on this
  module's own source). A viewer that needs the network is not an offline viewer, and a `src=` that nobody notices is
  how an evidence tool phones home.
* **the data cannot end its own script block.** `<` is escaped to `\\u003c` in the JSON payload, so a prompt containing
  `</script>` is text, not markup, and the XSS corpus in the tests renders as characters.
* **the capsule's own claims are labelled, not adopted.** `root_ok`, `problems` and the run's `status_cause` travel into
  the page, so the header can say "integrity FAILED" instead of drawing a confident timeline over bytes that do not
  match their own root.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blackbox.ui import model

STATIC = Path(__file__).resolve().parent / "static"
CSS_MARK = "<!--BBX_CSS-->"
DATA_MARK = "<!--BBX_DATA-->"
JS_MARK = "<!--BBX_JS-->"


def _static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def payload_json(document: dict[str, Any]) -> str:
    """The JSON block, with `<` escaped so no payload can close the script element it lives in."""
    return json.dumps(document, sort_keys=True, ensure_ascii=False).replace("<", "\\u003c")


def build_html(document: dict[str, Any]) -> str:
    """Inline the stylesheet, the script and the data into the template. One file, no references out."""
    html = _static("index.html")
    for mark, replacement in ((CSS_MARK, f"<style>\n{_static('viewer.css')}\n</style>"),
                              (JS_MARK, f"<script>\n{_static('app.js')}\n</script>"),
                              (DATA_MARK, payload_json(document))):
        if mark not in html:
            raise RuntimeError(f"the viewer template lost its {mark} marker")
        html = html.replace(mark, replacement)
    return html


def view(path: str, out: str | None = None) -> dict[str, Any]:
    """Render `path` (a capsule or a data directory) and write the page. Returns what was written and from what."""
    document = model.document_for_path(path)
    html = build_html(document)
    destination = Path(out) if out else Path(path).with_suffix(".html")
    if Path(path).is_dir() and out is None:
        destination = Path(path) / "viewer.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, not `write_text`: text mode translates `\n` to `\r\n` on Windows, and this file is an evidence artifact
    # whose whole point is that it is the same page everywhere it is opened.
    destination.write_bytes(html.encode("utf-8"))
    tapes = document.get("tapes") or [document["viewer"]]
    return {"out": str(destination), "bytes": len(html.encode("utf-8")),
            "run_id": (document.get("run") or {}).get("run_id"),
            "events": len(document.get("events") or []),
            "nodes": len(document.get("nodes") or []),
            "tapes": len(tapes), "capsule": document["viewer"].get("capsule_path")}


def main(argv: list[str]) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="blackbox view",
                                 description="render a capsule (or a data directory of them) as one offline HTML file")
    ap.add_argument("path", help="a .bbx capsule, or a data directory holding capsules")
    ap.add_argument("--out", default=None, help="where to write the page (default: beside the capsule)")
    ap.add_argument("--json", action="store_true", help="print what was written instead of a sentence")
    args = ap.parse_args(argv)
    try:
        result = view(args.path, args.out)
    except FileNotFoundError:
        print(f"blackbox view: {args.path} does not exist", file=sys.stderr)
        return 2
    except Exception as exc:                       # a capsule that cannot be read is a report, not a traceback
        print(f"blackbox view: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"blackbox: wrote {result['out']} ({result['bytes']} B) from {result['tapes']} tape(s) of "
              f"{result['run_id']} — {result['events']} events, {result['nodes']} nodes; open it with file://")
    return 0


__all__ = ["build_html", "main", "payload_json", "view"]
