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
| `/<token>/run/<capsule_id>` | one capsule, rendered by the existing viewer, with a bar back to the library |

Three decisions worth stating:

* **Server-rendered, no fetch.** Every page is one document with its CSS inlined; the only client-side script is the
  viewer's own keyboard tree on a run page. That is what lets the library work with the network stack idle and with
  JavaScript disabled.
* **A capsule id is matched, not joined.** `library.find()` compares ids against the manifests in the data directory; the
  URL never becomes a filesystem path, so `..` cannot become a request.
* **Everything is `GET`.** There is no route that writes, records or deletes. A browser that guesses the token can read
  the library, which is exactly what the token protects, and it cannot change anything.

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


# ── one run: the existing viewer, with a way back ───────────────────────────
BACK_BAR = ('<div style="position:fixed;left:14px;bottom:14px;z-index:20">'
            '<a class="navlink" href="{token}/library" style="text-decoration:none">← recorded runs</a></div>')


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
    bar = BACK_BAR.format(token=token)
    return page.replace("</body>", bar + "</body>")


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
            page = self._page(route, parse_qs(parsed.query))
        except Exception as exc:                    # a page that cannot be built is a 500 with the reason, not a hang
            self._send(500, "text/plain; charset=utf-8", f"{type(exc).__name__}: {exc}\n".encode())
            return
        if page is None:
            self._send(404, "text/html; charset=utf-8",
                       _inline(_static("library.html")).replace("<!--BBX_ONBOARDING-->", f"{token}/onboarding")
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
        return None

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
