"""The desktop app's pages, served on loopback at a per-process secret path.

`blackbox serve` answers exactly one request with one inlined page and stops. The desktop app needs more than that —
a library, an onboarding page, and a run page per capsule — while keeping the properties that made the one-shot server
defensible: **loopback only**, a **secret path generated per process and never logged**, no request that changes anything,
and no reference out of the page.

So this is the same server with routes:

| path | what it is |
|---|---|
| `/<token>/` | the library, or onboarding when there is nothing recorded yet |
| `/<token>/library?q=&status=&sort=&offset=` | the library, filtered and paginated — server-rendered, so it works with no script |
| `/<token>/onboarding` | first-run checks, the two lists (captured / not captured), and the next command |
| `/<token>/run/<capsule_id>` | one capsule, rendered by the existing viewer, with the action bar |
| `/<token>/verify/<name>` | recompute the integrity index and report every problem (writes nothing) |
| `/<token>/diagnose/<name>` | findings in the brief's own vocabulary, plus the brief as copyable text |
| `/<token>/repair?proposal=&repo=` | review a proposal: diff, risk, suggested tests, the approval token |
| `POST /<token>/repair/apply` | apply that proposal, after a ticked box **and** the typed token prefix |
| `POST /<token>/repair/tests` | run the commands the person chose, and report every exit code |
| `/<token>/result/<id>` | the outcome of one POST, addressed by an unguessable id, shown by a `GET` |

Three decisions worth stating:

* **Server-rendered, no fetch.** Every page is one document with its CSS inlined; the only client-side script is the
  viewer's own keyboard tree on a run page. That is what lets the library work with the network stack idle and with
  JavaScript disabled.
* **A capsule id is matched, not joined.** `library.find()` compares ids against the manifests in the data directory; the
  URL never becomes a filesystem path, so `..` cannot become a request.
* **`GET` cannot write.** Every page is a `GET`; the only two writes are `POST`s that take a ticked confirmation and, for
  an apply, the typed approval token bound to the proposal and the tree. A refresh cannot repeat one (the result is
  fetched by an unguessable id), another page's form cannot drive one (`Origin` must be this app, and the form must carry
  the per-process token), and a `GET` with any URL at all cannot write, record or delete anything.

Nothing here is exposed to the network: `HOST` is the loopback literal from `ui.server`, and `tests/test_ui_app.py`
asserts the bind tuple and that no route mutates the data directory.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from blackbox.ui import library as library_mod
from blackbox.ui import onboarding as onboarding_mod
from blackbox.ui.server import HOST, SCHEME

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
#: A page is built from files on disk; the app never asks for a second one. 8 MiB covers a run page with a large
#: capsule summary and is small enough that a stuck client cannot hold a thread for long.
MAX_PAGE = 8 << 20
#: A form is a handful of fields and, at most, a proposal's paths. 256 KiB is generous for that and small enough that a
#: stuck client cannot make the window read forever.
MAX_FORM = 256 << 10


def _static(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def _inline(html_text: str) -> str:
    """Put the palette, the app surfaces and (nothing else) into the page."""
    css = f"<style>\n{_static('viewer.css')}\n{_static('app.css')}\n</style>"
    return html_text.replace("<!--BBX_CSS-->", css)


# ── the library page ────────────────────────────────────────────────────────
def _badge(row: library_mod.Row) -> str:
    if not row.readable:
        return '<span class="badge bad">unreadable</span>'
    status = str(row.status or "unknown")
    kind = "ok" if status in library_mod.CLEAN_STATUSES else "warn"
    return f'<span class="badge {kind}">{html.escape(status)}</span>'


def _size(count: int | None) -> str:
    """A size a person reads: bytes under a kilobyte, one decimal above a megabyte."""
    value = float(count or 0)
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} kB"
    return f"{value / (1024 * 1024):.1f} MB"


def _noun(count: int, word: str) -> str:
    """The word for a tile, in the number the tile's value shows."""
    return word if count == 1 else f"{word}s"


def _tiles(found: library_mod.Library) -> str:
    counts = found.counts
    tiles = [
        (_noun(counts.get("capsules", 0), "capsule"), counts.get("capsules", 0), False),
        (_noun(counts.get("runs", 0), "run"), counts.get("runs", 0), False),
        ("events", counts.get("events", 0), False),
        ("need attention", counts.get("attention", 0), True),
        ("on disk", _size(counts.get("bytes", 0)), False),
    ]
    cells = "".join(
        f'<div class="tile{" attention" if attention else ""}"><div class="n">{html.escape(str(value))}</div>'
        f'<div class="k">{html.escape(label)}</div></div>'
        for label, value, attention in tiles)
    return f'<section class="tiles" aria-label="What is recorded">{cells}</section>'


def _search(found: library_mod.Library) -> str:
    statuses = "".join(
        f'<option value="{html.escape(name)}"{" selected" if name == found.status else ""}>'
        f'{html.escape(name)} ({count})</option>'
        for name, count in sorted(found.statuses.items()))
    sorts = "".join(
        f'<option value="{name}"{" selected" if name == found.sort else ""}>{name}</option>'
        for name in library_mod.SORTS)
    return f"""<section class="panel">
  <h2>Find a run</h2>
  <p class="lede">Search matches the run, the capsule, the provider, the model and the status. Everything here is read
  from this machine's capsules; nothing is sent anywhere.</p>
  <form class="search" method="get" action="library">
    <label>search <input type="search" name="q" value="{html.escape(found.query)}" placeholder="run id, provider, model, status" autofocus></label>
    <label>status <select name="status"><option value="">any ({len(found.statuses)} kinds)</option>{statuses}</select></label>
    <label>sort <select name="sort">{sorts}</select></label>
    <button type="submit">Show</button>
  </form>
</section>"""


def _rows(found: library_mod.Library, token: str) -> str:
    if not found.total:
        return ("""<section class="panel empty">
  <h2>Nothing recorded yet</h2>
  <p>Start your app through BlackBox once and the run appears here. The setup page has the exact command for the SDK you
  have installed.</p>
</section>""")
    head = ("<tr><th scope=\"col\">Run</th><th scope=\"col\">Status</th><th scope=\"col\">Recorded</th>"
            "<th scope=\"col\">Captured</th><th scope=\"col\" class=\"num\">Events</th><th scope=\"col\" class=\"num\">Size</th>"
            "<th scope=\"col\">Needs attention</th></tr>")
    body = []
    for row in found.rows:
        title = html.escape(str(row.capsule_id or row.name))
        href = "" if not row.readable else f"{token}/run/{html.escape(row.name)}"
        name = f'<a class="run" href="{href}">{title}</a>' if href else f'<span class="run">{title}</span>'
        # The file name is what a row is addressed by, and two files can hold one capsule: it belongs in the row.
        sub = html.escape(f"{row.run_id or 'no run id'} · {row.name}")
        if not row.readable:
            providers = "—"                        # nothing was read, so nothing may be claimed about what was captured
        elif row.providers:
            providers = ", ".join(row.providers)
        elif row.model_calls:
            providers = f"{row.model_calls} model call(s), provider not attributed"
        else:
            providers = "no model call recorded"
        models = ", ".join(row.models)
        captured = html.escape(providers) + (f'<span class="sub">{html.escape(models)}</span>' if models else "")
        reasons = "".join(f"<li>{html.escape(reason)}</li>" for reason in row.attention)
        attention = f'<ul class="reasons">{reasons}</ul>' if reasons else '<span class="badge ok">none</span>'
        size = _size(row.bytes)
        body.append(
            f"<tr><td>{name}<span class=\"sub\">{sub}</span></td><td>{_badge(row)}</td>"
            f"<td>{html.escape(row.when)}</td><td>{captured}</td>"
            f"<td class=\"num\">{html.escape(str(row.events if row.events is not None else '?'))}</td>"
            f"<td class=\"num\">{html.escape(size)}</td><td>{attention}</td></tr>")
    caption = (f"{found.total} run{'s' if found.total != 1 else ''}"
               + (f" matching “{html.escape(found.query)}”" if found.query else ""))
    return (f'<section class="panel"><table class="runs"><caption>{caption}</caption>'
            f"<thead>{head}</thead><tbody>{''.join(body)}</tbody></table></section>")


def _pager(found: library_mod.Library) -> str:
    if found.total <= found.limit:
        return ""
    def link(label: str, offset: int, disabled: bool) -> str:
        query = f"q={html.escape(found.query)}&status={html.escape(found.status)}&sort={found.sort}&offset={offset}"
        if disabled:
            return f'<a href="library?{query}" aria-disabled="true">{label}</a>'
        return f'<a href="library?{query}">{label}</a>'
    previous = link("← newer" if found.sort == "newest" else "← previous", max(0, found.offset - found.limit),
                    found.offset == 0)
    following = link("older →" if found.sort == "newest" else "next →", found.offset + found.limit,
                     found.offset + found.limit >= found.total)
    return (f'<nav class="pager" aria-label="Pages">{previous}'
            f'<span>page {found.page} of {found.pages}</span>{following}</nav>')


def library_page(data_dir: str, token: str, query: dict[str, list[str]]) -> str:
    def first(name: str, default: str = "") -> str:
        return (query.get(name) or [default])[0]

    found = library_mod.scan(data_dir, query=first("q"), status=first("status"),
                             sort=first("sort", "newest"),
                             limit=int(first("limit", "25") or 25), offset=int(first("offset", "0") or 0))
    page = _inline(_static("library.html"))
    replacements = {
        "<!--BBX_ONBOARDING-->": f"{token}/onboarding",
        "<!--BBX_REPAIR-->": f"{token}/repair",
        "<!--BBX_TILES-->": _tiles(found),
        "<!--BBX_SEARCH-->": _search(found),
        "<!--BBX_ROWS-->": _rows(found, token),
        "<!--BBX_PAGER-->": _pager(found),
    }
    for mark, value in replacements.items():
        page = page.replace(mark, value)
    return page


# ── the onboarding page ─────────────────────────────────────────────────────
def _check_item(check: dict[str, Any]) -> str:
    badge = {"ok": "ok", "info": "", "action": "warn", "warn": "warn", "fail": "bad"}.get(check["severity"], "")
    command = f'<div><code class="cmd">{html.escape(check["command"])}</code></div>' if check["command"] else ""
    docs = (f'<div class="sub"><a href="#">see {html.escape(check["docs"])}</a></div>' if check["docs"] else "")
    return (f'<li><span class="badge {badge}">{html.escape(check["severity"])}</span>'
            f'<span class="title">{html.escape(check["title"])}</span>'
            f'<span class="detail">{html.escape(check["detail"])}</span>{command}{docs}</li>')


def onboarding_page(data_dir: str, token: str) -> str:
    report = onboarding_mod.probe(data_dir)
    page = _inline(_static("onboarding.html"))
    checks = "".join(_check_item(check) for check in report["checks"])
    captured = "".join(f"<li>{html.escape(item)}</li>" for item in report["captured"])
    not_captured = "".join(f"<li>{html.escape(item)}</li>" for item in report["not_captured"])
    counts = report["counts"]
    replacements = {
        "<!--BBX_LIBRARY-->": f"{token}/library",
        "<!--BBX_VERDICT-->": (
            f'<section class="panel"><p class="verdict {html.escape(report["state"])}">{html.escape(report["summary"])}</p>'
            f'<p class="lede">{counts.get("ok", 0)} ok · {counts.get("action", 0)} to do · '
            f'{counts.get("warn", 0)} warnings · {counts.get("fail", 0)} blocking</p></section>'),
        "<!--BBX_CHECKS-->": (f'<section class="panel"><h2>What this machine can do</h2>'
                              f'<ul class="checks">{checks}</ul></section>'),
        "<!--BBX_POLICY-->": (f'<section class="panel"><h2>What is recorded — and what is not</h2>'
                              f'<div class="two-col">'
                              f'<div class="yes"><h3>Recorded</h3><ul>{captured}</ul></div>'
                              f'<div class="no"><h3>Not recorded</h3><ul>{not_captured}</ul></div>'
                              f"</div></section>"),
        "<!--BBX_DATA_DIR-->": html.escape(report["data_dir"]),
    }
    for mark, value in replacements.items():
        page = page.replace(mark, value)
    return page


# ── one run: the existing viewer, with the four things a person does next ───
BACK_BAR = ('<div class="runbar">'
            '<a class="navlink" href="{token}/library">← recorded runs</a>'
            '<a class="navlink" href="{token}/verify/{name}">Check this capsule</a>'
            '<a class="navlink" href="{token}/diagnose/{name}">Diagnose</a>'
            '<a class="navlink" href="{token}/repair">Repair a failed run</a>'
            '</div>')


def run_page(data_dir: str, token: str, name: str, *, single: str | None = None) -> str | None:
    from blackbox.ui.model import document_for_capsule
    from blackbox.ui.viewer import build_html

    path = library_mod.find_by_name(data_dir, name)
    if path is None and single and os.path.isfile(single):
        # A capsule named directly on the command line, outside any data directory: the one run the app was asked for.
        path = single if os.path.basename(single) == name else None
    if path is None:
        return None
    page = build_html(document_for_capsule(path))
    bar = BACK_BAR.format(token=token, name=html.escape(name))
    return page.replace("</body>", bar + "</body>")


# ── check, diagnose, repair: three pages and one gate ──────────────────────
def _shell(template: str, *, title: str, subtitle: str, token: str, body: str, footer: str,
           run: str = "", extra: dict[str, str] | None = None) -> str:
    page = _inline(_static(template))
    replacements = {
        "<!--BBX_TITLE-->": html.escape(title),
        "<!--BBX_SUBTITLE-->": html.escape(subtitle),
        "<!--BBX_LIBRARY-->": f"{token}/library",
        "<!--BBX_ONBOARDING-->": f"{token}/onboarding",
        "<!--BBX_RUN-->": f"{token}/{run}" if run else f"{token}/library",
        "<!--BBX_BODY-->": body,
        "<!--BBX_FOOTER-->": footer,
    }
    replacements.update(extra or {})
    for mark, value in replacements.items():
        page = page.replace(mark, value)
    return page


def verify_page(data_dir: str, token: str, name: str, *, single: str | None = None) -> str | None:
    """Recompute the capsule's integrity index and show every problem it reports, or the clean root."""
    from blackbox.ui import flows

    path = library_mod.find_by_name(data_dir, name)
    if path is None and single and os.path.basename(single) == name:
        path = single
    if path is None:
        return None
    result = flows.verify_capsule(path)
    if result.error:
        body = (f'<section class="panel"><h2>This capsule cannot be checked</h2>'
                f'<p class="lede">It was refused, which is an answer: {html.escape(result.error)}</p></section>')
    else:
        state = "ok" if result.ok else "bad"
        verdict = ("the integrity index recomputed exactly as written" if result.ok
                   else "the capsule does not verify")
        problems = ("".join(f"<li>{html.escape(problem)}</li>" for problem in result.problems)
                    or "<li>none</li>")
        body = (
            f'<section class="panel"><div class="verdict-line"><span class="big {state}">'
            f'{"verified" if result.ok else "failed"}</span><span class="badge {state}">'
            f'{html.escape(result.name)}</span></div>'
            f'<p class="lede">{html.escape(verdict)}. Recomputed here, from the bytes in the file: '
            f'{result.entries} entries, {_size(result.bytes)}, in {result.seconds:.0f} ms.</p>'
            f'<dl class="facts"><dt>integrity root</dt><dd>{html.escape(str(result.root or "—"))}</dd>'
            f'<dt>problems</dt><dd><ul class="plain">{problems}</ul></dd></dl></section>')
    return _shell("inspect.html", title=f"check · {name}", subtitle="integrity check · read-only",
                  token=token, body=body, run=f"run/{html.escape(name)}",
                  footer="This is the same work `blackbox verify` does. It writes nothing and changes nothing.")


def diagnose_page(data_dir: str, token: str, name: str, *, single: str | None = None) -> str | None:
    """The findings, in the brief's three voices, plus the brief itself as text a person can copy."""
    from blackbox.ui import flows

    path = library_mod.find_by_name(data_dir, name)
    if path is None and single and os.path.basename(single) == name:
        path = single
    if path is None:
        return None
    view = flows.diagnose_capsule(path)
    if view.error:
        body = (f'<section class="panel"><h2>There is nothing to diagnose yet</h2>'
                f'<p class="lede">{html.escape(view.error)}</p></section>')
    else:
        empty_note = "" if view.findings else '<p class="lede">No finding fired.</p>'
        cards = []
        for finding in view.findings:
            kind = {"observed": "ok", "likely": "warn", "possible": "warn", "cannot-tell": ""}.get(
                finding["verdict"], "")
            evidence = "".join(f"<li>{html.escape(item)}</li>" for item in finding["evidence"])
            alternatives = "".join(f"<li>{html.escape(item)}</li>" for item in finding["alternatives"])
            checks = "".join(f"<li>{html.escape(item)}</li>" for item in finding["checks"])
            blocks = []
            if evidence:
                blocks.append(f'<p class="label">evidence</p><ul>{evidence}</ul>')
            if alternatives:
                blocks.append(f'<p class="label">would also fit</p><ul>{alternatives}</ul>')
            if checks:
                blocks.append(f'<p class="label">what would settle it</p><ul>{checks}</ul>')
            cards.append(
                f'<article class="finding"><div class="row"><span class="badge {kind}">'
                f'{html.escape(finding["verdict"])}</span><span class="badge">{html.escape(finding["confidence"])}'
                f'</span></div><h3>{html.escape(finding["title"])}</h3>'
                f'<p>{html.escape(finding["basis"])}</p>{"".join(blocks)}</article>')
        body = (
            f'<section class="panel"><h2>What the evidence supports</h2>'
            f'<p class="verdict">{html.escape(view.headline)}</p>'
            f'<p class="lede">{html.escape(view.summary)} · {len(view.findings)} finding(s), read from the '
            f'capsule in {view.seconds:.0f} ms. A finding is never upgraded: `observed` means the frames show it, '
            f'`likely` names what would settle it.</p>{empty_note}'
            f'{"".join(cards)}</section>'
            f'<section class="panel"><h2>The repair brief</h2>'
            f'<p class="lede">This is the document you would hand to an assistant, a colleague or your own editor. '
            f'The app does not send it anywhere: copy it if you want to use it.</p>'
            f'<pre class="pane">{html.escape(view.brief)}</pre>'
            f'<p class="lede">Saving it is your decision and your file:'
            f' <a class="navlink" href="{token}/export/{html.escape(name)}/brief">save the brief</a>'
            f' <a class="navlink" href="{token}/export/{html.escape(name)}/report">save the report</a></p></section>')
    return _shell("inspect.html", title=f"diagnose · {name}", subtitle="diagnosis · read-only",
                  token=token, body=body, run=f"run/{html.escape(name)}",
                  footer="Nothing here contacts another program or another machine, and the brief stays on this "
                         "computer until you copy it.")


REPAIR_FORM = """<section class="panel">
  <h2>Which proposal, against which repository?</h2>
  <p class="lede">A proposal is a JSON file someone (often an assistant) wrote. It is a suggestion: this page shows you
  exactly what it would change, and it changes nothing until you approve it below. The repository is the directory the
  proposal's paths are relative to.</p>
  <form class="search" method="get" action="repair">
    <label>proposal file <input type="text" name="proposal" value="{proposal}" placeholder="/path/to/proposal.json"
      style="min-width:340px"></label>
    <label>repository <input type="text" name="repo" value="{repo}" placeholder="/path/to/your/repo"
      style="min-width:300px"></label>
    <button type="submit">Review it</button>
  </form>
</section>"""


def repair_page(token: str, query: dict[str, list[str]]) -> str:
    """Preview a proposal, then offer the two gated actions: apply it, or run the tests that would verify it."""
    from blackbox.ui import flows

    proposal = (query.get("proposal") or [""])[0]
    repo = (query.get("repo") or [""])[0]
    body = REPAIR_FORM.format(proposal=html.escape(proposal), repo=html.escape(repo))
    footer = ("A preview loads the proposal and reads the files it names. Nothing is written by this page load.")
    if not proposal or not repo:
        body += ('<section class="panel"><h2>Nothing loaded yet</h2>'
                 '<p class="lede">Name a proposal and the repository it should be applied to. '
                 '<code>blackbox repair preview</code> prints the same sheet in a terminal.</p></section>')
        return _shell("repair.html", title="repair review", subtitle="repair review · read-only until approved",
                      token=token, body=body, footer=footer)

    view = flows.review_proposal(proposal, repo)
    if not view.ok:
        body += (f'<section class="panel"><h2>This proposal cannot be reviewed</h2>'
                 f'<p class="lede">{html.escape(str(view.error))}</p>'
                 f'<p class="lede">Nothing was written. Fix the proposal or the repository path and try again.</p>'
                 f'</section>')
        return _shell("repair.html", title="repair review", subtitle="repair review · read-only until approved",
                      token=token, body=body, footer=footer)

    scope = view.scope
    files = "".join(
        f'<details class="file"><summary>{html.escape(change["verb"])} · {html.escape(change["path"])} '
        f'<span class="badge">{change["new_bytes"]} B</span></summary>'
        f'<pre class="pane diff">{_diff_html(change["diff"])}</pre></details>'
        for change in view.changes)
    risk = "".join(f"<li>{html.escape(note)}</li>" for note in view.risk) or "<li>none stated</li>"
    tests = "".join(f"<li><code>{html.escape(test)}</code></li>" for test in view.tests) or "<li>none suggested</li>"
    body += (
        f'<section class="panel"><h2>What this proposal does</h2>'
        f'<p class="verdict">{html.escape(view.title)}</p>'
        f'<p class="lede">{html.escape(view.rationale)}</p>'
        f'<dl class="facts"><dt>repository</dt><dd>{html.escape(repo)}</dd>'
        f'<dt>scope</dt><dd>{scope.get("files", 0)} file(s), +{scope.get("added_lines", 0)} / '
        f'-{scope.get("removed_lines", 0)} line(s)</dd>'
        f'<dt>creates</dt><dd>{html.escape(", ".join(scope.get("creates") or []) or "—")}</dd>'
        f'<dt>replaces</dt><dd>{html.escape(", ".join(scope.get("replaces") or []) or "—")}</dd></dl>'
        f'<h2 style="margin-top:18px">Why it might be a bad idea</h2><ul class="plain">{risk}</ul>'
        f'<h2 style="margin-top:18px">Tests it suggests — <strong>not run by this page</strong></h2>'
        f'<ul class="plain">{tests}</ul></section>'
        f'<section class="panel"><h2>The exact changes</h2>'
        f'<p class="lede">Each file, as a unified diff against what is on disk right now. Nothing below is applied until '
        f'the form at the foot of this page is filled in.</p>{files}</section>')

    body += (
        f'<section class="panel danger"><h2>Approve this change</h2>'
        f'<p class="gate">Applying writes the {scope.get("files", 0)} file(s) above into <strong>'
        f'{html.escape(repo)}</strong>, after copying every file it replaces into a verified backup directory inside '
        f'that repository. It never runs anything the proposal contains.</p>'
        f'<p class="gate">The approval token for <em>this diff against this tree</em> begins <strong>'
        f'{html.escape(view.hint)}</strong>. Type those eight characters below. If any file changed since this sheet '
        f'was drawn, the token no longer matches and the apply refuses instead of overwriting work you have not seen.'
        f'</p>'
        f'<form method="post" action="repair/apply">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="proposal" value="{html.escape(proposal)}">'
        f'<input type="hidden" name="repo" value="{html.escape(repo)}">'
        f'<label class="check"><input type="checkbox" name="confirm" value="yes" required>'
        f'<span>I read the diff above and I want exactly these {scope.get("files", 0)} file(s) changed.</span></label>'
        f'<label class="field">approval token (first eight characters)'
        f'<input type="text" name="typed" autocomplete="off" spellcheck="false" maxlength="8" required></label>'
        f'<button type="submit">Apply exactly this</button></form>'
        f'<p class="lede">Or run the tests that would show whether it worked — this <strong>executes commands</strong> '
        f'in the repository, so it is a separate button and a separate decision.</p>'
        f'<form method="post" action="repair/tests">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="repo" value="{html.escape(repo)}">'
        + "".join(f'<input type="hidden" name="command" value="{html.escape(test)}">' for test in view.tests)
        + f'<label class="check"><input type="checkbox" name="confirm" value="yes" required>'
        f'<span>Run these {len(view.tests)} command(s) in {html.escape(repo)} and show me the exit codes.</span></label>'
        f'<button type="submit">Run the tests</button></form></section>')
    return _shell("repair.html", title="repair review", subtitle="repair review · read-only until approved",
                  token=token, body=body, footer=footer)


def _diff_html(diff: str) -> str:
    """A diff, coloured by line, every character escaped — a proposal is untrusted text and never becomes markup."""
    out = []
    for line in diff.splitlines():
        escaped = html.escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f'<span class="add">{escaped}</span>')
        elif line.startswith("-") and not line.startswith("---"):
            out.append(f'<span class="del">{escaped}</span>')
        else:
            out.append(escaped)
    return "\n".join(out)


#: Results of a POST, addressed by an unguessable id and shown by a GET, so a refresh cannot repeat the action. Bounded:
#: a session that posts fifty times keeps the last thirty-two answers.
RESULTS_MAX = 32


def _result_page(token: str, kind: str, payload: dict[str, Any]) -> str:
    """Render the outcome of an apply or a test run, in the same voices the CLI uses."""
    back = f'<p><a class="navlink" href="{token}/library">← recorded runs</a></p>'
    if payload.get("refused"):
        body = (f'<section class="panel danger"><h2>Refused — nothing changed</h2>'
                f'<p class="lede">{html.escape(str(payload["refused"]))}</p>{back}</section>')
    elif kind == "apply":
        created = "".join(f"<li>{html.escape(path)}</li>" for path in payload.get("created") or []) or "<li>none</li>"
        written = "".join(f"<li>{html.escape(path)}</li>" for path in payload.get("written") or []) or "<li>none</li>"
        body = (f'<section class="panel"><h2>Applied</h2>'
                f'<p class="lede">The files below were written, and the originals were copied into a backup directory '
                f'first. The backup is {html.escape(str(payload.get("backup")))}'
                + (' (private by mode)' if payload.get("private_by_mode") else
                   ' (this platform has no POSIX modes: as private as the repository ACLs)') +
                f'.</p><dl class="facts"><dt>manifest</dt>'
                f'<dd>{html.escape(str(payload.get("manifest")))}</dd>'
                f'<dt>created</dt><dd><ul class="plain">{created}</ul></dd>'
                f'<dt>replaced</dt><dd><ul class="plain">{written}</ul></dd></dl>'
                f'<p class="lede">Applying is not verification. Run the tests that show whether it worked, or '
                f'<code>blackbox repair verify</code> with commands you choose.</p>{back}</section>')
    else:
        rows = "".join(
            f'<tr><td><code>{html.escape(str(row.get("command")))}</code></td>'
            f'<td class="num">{row.get("exit_code")}</td><td class="num">{row.get("seconds")}s</td></tr>'
            for row in payload.get("ran") or [])
        verdict = "passed" if payload.get("verified") else "not verified"
        kind_class = "ok" if payload.get("verified") else "bad"
        tails = "".join(
            f'<h3 style="font-size:13px;color:var(--gold)">{html.escape(str(row.get("command")))}</h3>'
            f'<pre class="pane">{html.escape(chr(10).join(row.get("tail") or []) or "(no output)")}</pre>'
            for row in payload.get("ran") or [])
        body = (f'<section class="panel"><h2>Verification: <span class="badge {kind_class}">{verdict}</span></h2>'
                f'<p class="lede">{html.escape(str(payload.get("note")))}</p>'
                f'<table class="runs"><thead><tr><th scope="col">command</th><th scope="col" class="num">exit</th>'
                f'<th scope="col" class="num">time</th></tr></thead><tbody>{rows}</tbody></table>'
                f'{tails}{back}</section>')
    return _shell("inspect.html", title="result", subtitle="the result of what you approved", token=token,
                  body=body, footer="This page is shown once per action; reloading it does not repeat anything.")


# ── the server ─────────────────────────────────────────────────────────────
class AppServer:
    """A loopback app server: pages under a secret path, no state changes, nothing logged."""

    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread, token: str) -> None:
        self._httpd = httpd
        self._thread = thread
        self.token = token
        self.port = int(httpd.server_address[1])
        self.endpoint = f"{HOST}:{self.port}"
        self.url = f"{SCHEME}://{HOST}:{self.port}/{token}/"

    @property
    def requests(self) -> int:
        """How many pages have been served. Read from the socket server, so it is the truth and not a copy."""
        return int(getattr(self._httpd, "requests", 0))

    @property
    def last_render_ms(self) -> float:
        return float(getattr(self._httpd, "last_render_ms", 0.0))

    def wait_until_served(self, timeout_s: float = 0.5) -> None:
        deadline = time.monotonic() + timeout_s
        while self.requests == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    def stop(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


class _Handler(BaseHTTPRequestHandler):
    server_version = "BlackBoxApp/1"
    protocol_version = "HTTP/1.1"
    data_dir: str = ""

    @property
    def token(self) -> str:
        return str(getattr(self.server, "token", ""))

    def do_GET(self) -> None:                      # BaseHTTPRequestHandler's spelling, not ours
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        token = self.token
        if not parts or not secrets.compare_digest(parts[0], token):
            self._send(404, "text/plain; charset=utf-8", b"not found\n")
            return
        route = parts[1:]
        started = time.perf_counter()
        # Counted here, before the page is built: a client that sees its last byte has already finished, and a counter
        # read from the caller must never be behind the response it just received (a race the full suite found).
        if isinstance(self.server, _App):
            self.server.requests += 1
        try:
            if len(route) == 3 and route[0] == "export" and self._export(route[1], route[2]):
                return
            page = self._page(route, parse_qs(parsed.query))
        except Exception as exc:                    # a page that cannot be built is a 500 with the reason, not a hang
            self._send(500, "text/plain; charset=utf-8", f"{type(exc).__name__}: {exc}\n".encode())
            return
        if page is None:
            self._send(404, "text/html; charset=utf-8",
                       _inline(_static("library.html")).replace("<!--BBX_ONBOARDING-->", f"{token}/onboarding")
                       .replace("<!--BBX_REPAIR-->", f"{token}/repair")
                       .replace("<!--BBX_TILES-->", "")
                       .replace("<!--BBX_SEARCH-->", "")
                       .replace("<!--BBX_ROWS-->", '<section class="panel empty"><h2>No such run</h2>'
                                                   '<p>That capsule is not in this data directory any more.</p></section>')
                       .replace("<!--BBX_PAGER-->", "").encode())
            return
        body = page.encode("utf-8")
        if len(body) > MAX_PAGE:
            self._send(500, "text/plain; charset=utf-8", b"page too large\n")
            return
        self._send(200, "text/html; charset=utf-8", body)
        if isinstance(self.server, _App):
            self.server.last_render_ms = (time.perf_counter() - started) * 1000.0

    def _export(self, name: str, kind: str) -> bool:
        """Hand the person a document as a file. Reads the capsule, writes nothing on this machine.

        An `attachment` download is the one way a page can leave a file behind, and it is the person's own browser doing
        it at their own click — the app's promise is that *it* writes nothing, and this keeps it.
        """
        from blackbox.ui import flows

        data_dir = getattr(self.server, "data_dir", self.data_dir)
        path = library_mod.find_by_name(data_dir, name)
        single = getattr(self.server, "single", None)
        if path is None and single and os.path.basename(single) == name:
            path = single
        if path is None:
            return False
        view = flows.diagnose_capsule(path)
        if view.error:
            return False
        text, suffix = (view.brief, "brief.md") if kind == "brief" else (view.markdown, "report.md") if kind == "report" \
            else ("", "")
        if not text:
            return False
        filename = f"{name.rsplit('.', 1)[0]}-{suffix}"
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(text.encode("utf-8"))))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))
        return True

    def _page(self, route: list[str], query: dict[str, list[str]]) -> str | None:
        data_dir = getattr(self.server, "data_dir", self.data_dir)
        token = self.token
        if not route:
            if getattr(self.server, "force_onboarding", False):
                return onboarding_page(data_dir, token)
            if library_mod.scan(data_dir, limit=1).counts.get("capsules"):
                return library_page(data_dir, token, query)
            return onboarding_page(data_dir, token)
        head, rest = route[0], route[1:]
        if head == "library":
            return library_page(data_dir, token, query)
        if head == "onboarding":
            return onboarding_page(data_dir, token)
        if head == "run" and rest:
            return run_page(data_dir, token, rest[0], single=getattr(self.server, "single", None))
        if head == "verify" and rest:
            return verify_page(data_dir, token, rest[0], single=getattr(self.server, "single", None))
        if head == "diagnose" and rest:
            return diagnose_page(data_dir, token, rest[0], single=getattr(self.server, "single", None))
        if head == "repair":
            # `repair` alone is the review page; `repair/apply` and `repair/tests` are `POST`-only, so a `GET` on either
            # is a 404 — nobody should be able to think a link applied something (tests/test_ui_safety.py).
            return repair_page(token, query) if not rest else None
        if head == "result" and rest:
            return self._stored_result(token, rest[0])
        if head == "export" and len(rest) == 2:
            return None                          # handled by `_export`, which writes a file rather than a page
        return None

    def _stored_result(self, token: str, result_id: str) -> str | None:
        """A POST's outcome, looked up by id. A GET cannot cause a write, whatever is in the URL bar."""
        results = getattr(self.server, "results", None)
        if results is None:
            return None
        stored = results.get(result_id)
        if stored is None:
            return None
        kind, payload = stored
        return _result_page(token, kind, payload)

    def do_POST(self) -> None:
        """The only two writes the app can make, behind four checks and one human.

        `POST` exists for exactly two actions — apply a reviewed proposal, and run the commands a person chose — and the
        checks are ordered so the cheapest refusal comes first:

        1. **the path token**, constant-time compared, so another program on this machine cannot find the route;
        2. **`Origin`**, which must be this app's own origin (or absent, for a non-browser client the user started). A
           form posted from any other page is refused, which is the CSRF guard;
        3. **the `csrf` field**, which must equal the same per-process token — so the action cannot be driven by a link;
        4. **the body**, capped at `MAX_FORM`, parsed only as `application/x-www-form-urlencoded`.

        The human gate is inside `flows`: an unticked confirmation or a mistyped approval token refuses before anything
        is written.
        """
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        token = self.token
        if not parts or not secrets.compare_digest(parts[0], token):
            self._send(404, "text/plain; charset=utf-8", b"not found\n")
            return
        if not self._origin_allowed():
            self._send(403, "text/plain; charset=utf-8", b"cross-origin form posts are refused\n")
            return
        form = self._read_form()
        if form is None:
            self._send(400, "text/plain; charset=utf-8", b"body must be one urlencoded form\n")
            return
        if not secrets.compare_digest((form.get("csrf") or [""])[0], token):
            self._send(403, "text/plain; charset=utf-8", b"missing or wrong form token\n")
            return
        from blackbox.ui import flows

        route = parts[1:]
        if isinstance(self.server, _App):
            self.server.requests += 1
        try:
            if route == ["repair", "apply"]:
                payload = self._apply_from_form(form)
                kind = "apply"
            elif route == ["repair", "tests"]:
                payload = self._tests_from_form(form)
                kind = "tests"
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
                return
        except flows.FlowRefused as refusal:
            payload, kind = {"refused": str(refusal)}, "refused"
        except Exception as exc:                    # a failure is reported as a result page, not a stack trace
            payload, kind = {"refused": f"{type(exc).__name__}: {exc}"}, "refused"
        result_id = secrets.token_urlsafe(9)
        results = getattr(self.server, "results", None)
        if results is not None:
            results[result_id] = (kind, payload)
            while len(results) > RESULTS_MAX:
                results.pop(next(iter(results)))
        self._redirect(f"{token}/result/{result_id}")

    def _origin_allowed(self) -> bool:
        """Accept this app's own origin, and a client that sends none. Anything else is another page's form."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        expected = f"{SCHEME}://{HOST}:{int(getattr(self.server, 'server_address', (HOST, 0))[1])}"
        return origin.rstrip("/") == expected

    def _read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_FORM:
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return None
        body = self.rfile.read(length)
        return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

    @staticmethod
    def _apply_from_form(form: dict[str, list[str]]) -> dict[str, Any]:
        from blackbox.ui import flows

        def first(name: str) -> str:
            return (form.get(name) or [""])[0]

        return flows.apply_reviewed(first("proposal"), first("repo"), typed=first("typed"),
                                    confirmed=first("confirm") == "yes")

    @staticmethod
    def _tests_from_form(form: dict[str, list[str]]) -> dict[str, Any]:
        from blackbox.ui import flows

        def first(name: str) -> str:
            return (form.get(name) or [""])[0]

        return flows.run_tests([command for command in form.get("command") or [] if command.strip()],
                               cwd=first("repo"), confirmed=first("confirm") == "yes")

    def _redirect(self, location: str) -> None:
        """A `303` after a POST: the browser follows it with a GET, so a refresh cannot repeat the action."""
        self.send_response(303)
        self.send_header("Location", "/" + location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # The token is in the path, so an access log is a log of secrets. There is no log.
    def log_message(self, format: str, *args: Any) -> None:   # the base class's parameter names
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page is one document with its own styles; nothing may be loaded from elsewhere even if markup changed.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _App(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, token: str, data_dir: str, force_onboarding: bool,
                 single: str | None = None) -> None:
        self.token = token
        self.data_dir = data_dir
        self.force_onboarding = force_onboarding
        self.single = single
        self.requests = 0
        self.last_render_ms = 0.0
        self.results: dict[str, tuple[str, dict[str, Any]]] = {}
        super().__init__(address, _Handler)


def start(data_dir: str, *, port: int = 0, token: str | None = None, force_onboarding: bool = False,
          single: str | None = None) -> AppServer:
    """Bind loopback and return before anything is fetched. The token is generated here and never written to a log."""
    token = token or secrets.token_urlsafe(24)
    httpd = _App((HOST, int(port)), token=token, data_dir=data_dir, force_onboarding=force_onboarding, single=single)
    thread = threading.Thread(target=httpd.serve_forever, name="blackbox-app", daemon=True)
    thread.start()
    return AppServer(httpd, thread, token)


def _json_default(value: Any) -> str:            # pragma: no cover - convenience for callers printing state
    return str(value)


def describe(app: AppServer) -> dict[str, Any]:
    """What a caller (or `--json`) can report about the running page server."""
    return {"url": app.url, "port": app.port, "endpoint": app.endpoint, "requests": app.requests,
            "last_render_ms": round(app.last_render_ms, 1),
            "data_dir": getattr(app._httpd, "data_dir", ""),
            "onboarding": bool(getattr(app._httpd, "force_onboarding", False))}


def _dumps(value: Any) -> str:                   # pragma: no cover - small helper used by tests and callers
    return json.dumps(value, default=_json_default, sort_keys=True)
