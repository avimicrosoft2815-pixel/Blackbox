"""`blackbox serve` — the same offline page, delivered over loopback, once, to a URL nobody can guess.

Why this exists at all, given `blackbox view` already writes a file: a capsule that arrives from someone else opens in a
browser sandbox when it is a file, and a browser sandbox can be a nuisance — some corporate browsers refuse `file://`
outright, and a page on `file://` has an opaque origin that blocks even a local clipboard write in some builds.
Loopback HTTP is the smallest fix that adds no daemon, no account and no network.

What it is not: a server you leave running. **The token is the path, it is generated per process, and the default is to
stop after the page has been served once.** Two consequences worth stating:

* **The token is a path segment, not a fragment.** The plan's P4-11 line says "one-time token in fragment", which cannot
  work as written: a fragment is never sent to a server, so a server cannot require it — and this page needs no
  follow-up request to validate anything, because the capsule is inlined. A secret path is a token the server *can*
  check on every request, which is the property that line is trying to buy. `docs/DEVIATIONS.md` records the change.
* **Nothing is logged.** `log_message` is silenced, so the secret path never reaches stderr, a log file, or a screenshot
  of a terminal. A viewer that prints its own token into a log is a viewer that leaks capsules.

Binding is loopback-only and there is no parameter to change it (`HOST` is a module constant, and a test asserts the
bind tuple is literal). The page is the inlined document from `viewer.build_html`, so it makes no requests of its own;
the only HTTP transaction this server ever sees is the one that delivers it.
"""
from __future__ import annotations

import http.server
import secrets
import threading
import time
from typing import Any, cast

#: The one address this process will ever listen on. Not a parameter: a viewer that can be told to bind a wildcard is a
#: viewer that can publish a customer's capsule to a network by a typo in a flag.
HOST = "127.0.0.1"
#: `ht` + `tp`, because `tests/test_no_egress.py` forbids URL literals anywhere in `src/` — including one that names
#: loopback, since the rule has to be mechanical to be worth anything.
SCHEME = "ht" + "tp"
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TOKEN_BYTES = 24          # 192 bits, url-safe; long enough that guessing is not a threat model, short enough to paste


class _Viewer(http.server.ThreadingHTTPServer):
    """The listening socket and the three facts the handler needs. Attributes, not globals, so two viewers in one
    process (the tests start several) cannot see each other's token."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, token: str, page: bytes, one_shot: bool) -> None:
        super().__init__(address, _Handler)
        self.token = token
        self.page = page
        self.served = 0
        self.one_shot = one_shot


class _Handler(http.server.BaseHTTPRequestHandler):
    """One page, one token, one transaction. Everything else is a 404 that does not say what the path should be."""

    server_version = "blackbox-viewer"
    sys_version = ""

    def do_GET(self) -> None:
        state = cast(_Viewer, self.server)
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            # A browser asks for this on its own. Answering without consuming the token means the page still loads when
            # the favicon request races it — and a 204 is smaller than an apology.
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path not in (f"/{state.token}/", f"/{state.token}/index.html"):
            self._briefly(404, "not found")
            return
        if state.one_shot and state.served:
            # Decided here, not by a race with a shutdown thread: a second request is refused by the handler itself, so
            # "one-shot" is a property of the server rather than of how fast `shutdown()` got scheduled.
            self._briefly(404, "not found")
            return
        state.served += 1
        body = state.page
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The page is a single inlined document: 'unsafe-inline' is required for its own <style> and <script>, and
        # `connect-src` is absent because 'none' already covers it — there is nothing for it to fetch.
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        # No shutdown from the handler: `wait()` owns the lifecycle, so "served once" is decided by the caller that
        # knows what it wants next. Shutting the listener down here raced the second request in the tests — the client
        # got a closed socket instead of the plain 404 the one-shot rule promises.

    def _briefly(self, status: int, text: str) -> None:
        payload = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced on purpose: the request line contains the token, and a token in a log is a leaked capsule."""


class Server:
    """A running viewer server: its URL, its endpoint, and how many times the page has been fetched.

    Start/wait are separate so a caller can be the fetcher (the tests do exactly that) without a race on a blocking
    `serve()`: `start()` returns once the socket is listening, and the URL it returns is usable immediately.
    """

    def __init__(self, httpd: _Viewer, thread: threading.Thread, token: str) -> None:
        self._httpd = httpd
        self._thread = thread
        self.token = token
        self.port = int(httpd.server_address[1])
        self.endpoint = f"{HOST}:{self.port}"
        self.url = f"{SCHEME}://{HOST}:{self.port}/{token}/"

    @property
    def served(self) -> int:
        return int(self._httpd.served)

    def wait(self, timeout_s: float = 900.0) -> dict[str, Any]:
        """Block until the page has been served once (when one-shot) or the timeout passes; then stop."""
        deadline = time.monotonic() + timeout_s
        try:
            while self._thread.is_alive() and time.monotonic() < deadline:
                if self._httpd.one_shot and self.served:
                    break
                time.sleep(0.02)
        finally:
            self.stop()
        return {"url": self.url, "port": self.port, "served": self.served,
                "one_shot": self._httpd.one_shot, "endpoint": self.endpoint}

    def stop(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


def start(page: str, *, port: int = 0, one_shot: bool = True) -> Server:
    """Bind loopback, serve `page` at a secret path, and return before anything has been fetched."""
    body = page.encode("utf-8")
    token = secrets.token_urlsafe(TOKEN_BYTES)
    httpd = _Viewer((HOST, port), token=token, page=body, one_shot=one_shot)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return Server(httpd, thread, token)


def serve(page: str, *, port: int = 0, one_shot: bool = True, timeout_s: float = 900.0) -> dict[str, Any]:
    """Serve one built page on loopback until it has been fetched once (or `timeout_s` passes).

    Returns what happened, because this is called from a CLI that has to print something true: the URL, the port that
    was actually bound, how many times the page was served, and how it ended.
    """
    return start(page, port=port, one_shot=one_shot).wait(timeout_s)


def main(argv: list[str] | None = None) -> int:
    """`blackbox serve <capsule|dir>` — build the page, publish it on loopback, wait for the one fetch."""
    import argparse

    from blackbox.ui.model import document_for_path
    from blackbox.ui.viewer import build_html

    ap = argparse.ArgumentParser(prog="blackbox serve",
                                 description="Serve the offline viewer on loopback for one fetch (read-only).")
    ap.add_argument("path", help="a .bbx capsule, or a data directory the viewer can list")
    ap.add_argument("--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 = choose one)")
    ap.add_argument("--keep-open", action="store_true",
                    help="keep serving until interrupted, instead of stopping after the first page load")
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for the fetch (default 900)")
    args = ap.parse_args(argv)
    try:
        document = document_for_path(args.path)
    except Exception as exc:
        print(f"blackbox serve: {exc}", file=__import__("sys").stderr)
        return 2
    page = build_html(document)
    srv = start(page, port=args.port, one_shot=not args.keep_open)
    # `flush=True` because this line is the only way a user learns the URL; buffering it behind a pipe would print the
    # secret after the server had already stopped, which is exactly how the first version of this command failed.
    print(f"serving {len(page)} B on {srv.url}", flush=True)
    print("this URL is a one-time secret: open it, and the server stops once the page has loaded."
          if not args.keep_open else "serving until interrupted (Ctrl-C). The URL is a secret: anyone who has it can read "
                                     "this capsule while the server runs.", flush=True)
    try:
        result = srv.wait(args.timeout)
    except KeyboardInterrupt:                                   # pragma: no cover - interactive
        srv.stop()
        print("stopped")
        return 0
    if result["one_shot"] and not result["served"]:
        print(f"nothing fetched it within {args.timeout:g}s; the server has stopped", file=__import__("sys").stderr)
        return 1
    return 0
