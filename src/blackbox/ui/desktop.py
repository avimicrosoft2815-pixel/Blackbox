"""`blackbox desktop` — the offline viewer in a window of its own, not a browser tab.

**Why this and not a toolkit.** The viewer already exists: one self-contained HTML document, black and gold, keyboard
first, no network, whose contrast ratios and XSS handling are tested (`tests/test_ui_viewer.py`). The desktop shell's job
is to give that document a window, a title bar and a launcher icon — not to build a second UI in a second toolkit, which
would be two designs to keep honest and two places for a capsule to be rendered differently. So the shell embeds the
**system** webview: WebKitGTK on Linux, WebView2 (Edge) on Windows 10/11, neither of which we ship or bundle. That keeps
the download small, keeps the app offline, and keeps one renderer for the page whose rendering is already tested.

**The dependency, stated plainly.** The window needs `pywebview` (the project of that name on PyPI), which is **not** a
dependency of this package's core: `blackbox`'s core imports stdlib only and a test enforces it
(`tests/test_stdlib_only.py`). The shell imports `webview` lazily, inside the function that opens the window, and the
package declares it under one extra:

    pip install "blackbox-recorder[desktop]"

On Linux that is not quite enough — `pywebview` drives GTK through PyGObject, which is a distribution package, not a
wheel. The exact command for Debian/Ubuntu is printed by `blackbox desktop --selftest` when something is missing, and it
is in `docs/desktop.md`. On Windows the wheel is enough: WebView2 ships with Windows 10/11 (and the Edge "Evergreen"
runtime is what `pywebview` uses).

**What it will not do.** If the window cannot be opened it does **not** quietly fall back to a browser tab and call it a
desktop app: it exits 3, says which piece is missing, and prints the exact command. `blackbox serve` remains the
documented browser path for anyone who wants it.

**Security, in three lines.** The page is served over **loopback only** (`ui.server.HOST` is a module constant and its
literal is asserted by a test), at a **per-process secret path** that is never logged, and the window is created with
`debug=False` so no inspector is exposed. The capsule is inside the page; the only request the server ever answers is the
one that delivers it.
"""
from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any

from blackbox import __version__

#: What a user must install to get a window, per platform. Kept as data so `--selftest`, the error message and the docs
#: cannot drift apart.
INSTALL_HINT = {
    "linux": ('pip install "blackbox-recorder[desktop]" then '
              'sudo apt install -y python3-gi gir1.2-webkit2-4.1   # Debian/Ubuntu; '
              'Fedora: sudo dnf install python3-gobject webkit2gtk4.1'),
    "win32": 'pip install "blackbox-recorder[desktop]"   # WebView2 already ships with Windows 10/11',
    "darwin": 'pip install "blackbox-recorder[desktop]"   # macOS is experimental: untested in this build',
}

#: The window sizes the shell opens at, largest first. A viewer that opens at 900×600 on a 4K display is a viewer that
#: looks broken, and a viewer that opens full-screen hides the desktop the user is working on.
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (760, 520)


def _platform_key() -> str:
    return "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")


def _import_webview() -> Any:
    """The one import this module cannot make at the top of the file. Returns the module or None, never raises."""
    try:
        import webview  # type: ignore[import-not-found]  # the optional extra, imported where it is used
    except Exception:                                    # ImportError, and the OSError a broken GI typelib raises
        return None
    return webview


def _gtk_available() -> bool:
    """True when PyGObject and a WebKit typelib are importable — the two things a Linux wheel cannot bring."""
    try:
        import gi  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        gi.require_version("WebKit2", "4.1")
        gi.require_version("Gtk", "3.0")
    except Exception:
        try:
            gi.require_version("WebKit2", "4.0")
            gi.require_version("Gtk", "3.0")
        except Exception:
            return False
    return True


def selftest(webview_module: Any | None = None) -> dict[str, Any]:
    """What this machine can do, in the same shape `--selftest --json` prints.

    Every field is an observed fact or an explicit `None`: `window_backend` is `"pywebview"` only when the module
    imported, and `gtk_ready` is only meaningful on Linux.
    """
    key = _platform_key()
    module = webview_module if webview_module is not None else _import_webview()
    report: dict[str, Any] = {
        "blackbox": __version__,
        "platform": key,
        "machine": platform.machine(),
        "window_backend": "pywebview" if module is not None else None,
        "gtk_ready": _gtk_available() if key == "linux" else None,
        "install_hint": INSTALL_HINT[key],
        "available": False,
        "missing": [],
    }
    if module is None:
        report["missing"].append("pywebview")
    if key == "linux" and not report["gtk_ready"]:
        report["missing"].append("python3-gi + gir1.2-webkit2-4.1")
    report["available"] = not report["missing"]
    return report


def open_window(url: str, *, title: str = "BlackBox", webview_module: Any | None = None,
                start_kwargs: dict[str, Any] | None = None) -> None:
    """Open one window on `url` and block until the user closes it.

    `webview_module` exists for the test suite: a fake module records the window and returns from `start` immediately,
    which is how the wiring (loopback URL, title, no debug inspector, server stopped afterwards) is tested without a
    display. Production callers pass nothing.
    """
    module = webview_module if webview_module is not None else _import_webview()
    if module is None:                                   # pragma: no cover - guarded by the caller's selftest
        raise RuntimeError("no webview module")
    module.create_window(title, url, width=DEFAULT_SIZE[0], height=DEFAULT_SIZE[1],
                         min_size=MIN_SIZE, resizable=True, easy_drag=False, text_select=True)
    # `debug=False` is not decoration: with the inspector on, a keystroke in a shipped app opens a console that can read
    # `window.pywebview.api` and anything the page holds. The page holds a capsule.
    kwargs: dict[str, Any] = {"debug": False, "private_mode": True}
    kwargs.update(start_kwargs or {})
    module.start(**kwargs)
    return None


def run(path: str, *, port: int = 0, window: bool = True, as_json: bool = False,
        webview_module: Any | None = None, timeout_s: float = 900.0,
        announce: Any | None = None, onboarding: bool = False) -> dict[str, Any]:
    """Serve the app pages on loopback and show them in a window. Returns the report.

    The window opens on the **library** — or on onboarding when there is nothing recorded yet, because a first run that
    lands on an empty table teaches nothing. Given a capsule file rather than a directory, the window opens on that run
    and the library is one click away.

    With `window=False` this is the browser fallback: the URL is announced **before** the wait, and the process serves
    until it has been idle for `timeout_s` seconds. The first version waited for a fetch while the caller printed the URL
    afterwards — a fallback nobody could reach (defect 87).
    """
    from blackbox.ui import app as app_mod
    from blackbox.ui import library as library_mod

    start_route = ""
    single = None
    if os.path.isdir(path):
        data_dir = path
    else:
        # A capsule file: open the library it belongs to, with that run in front. If it belongs to no library, the app
        # still serves that one file by path — `blackbox desktop some/capsule.bbx` must work on a capsule alone.
        data_dir = library_mod.data_root(path)
        start_route = f"run/{os.path.basename(path)}"
        if library_mod.find_by_name(data_dir, os.path.basename(path)) is None:
            single = os.path.abspath(path)
    srv = app_mod.start(data_dir, port=port, force_onboarding=onboarding, single=single)
    url = srv.url + start_route
    found = library_mod.scan(data_dir, limit=1)
    report: dict[str, Any] = {
        "url": url,
        "window": window,
        "data_dir": os.path.abspath(data_dir),
        "capsules": found.counts.get("capsules", 0),
        "runs": found.counts.get("runs", 0),
        "events": found.counts.get("events", 0),
        "attention": found.counts.get("attention", 0),
        "requests": 0,
        "seconds": 0.0,
    }
    started = time.monotonic()
    try:
        if window:
            open_window(url, title="BlackBox — recorded runs", webview_module=webview_module)
        else:
            if announce is not None:
                announce(url)
            _serve_until_idle(srv, timeout_s)
    finally:
        report["requests"] = int(srv.requests)
        report["seconds"] = round(time.monotonic() - started, 2)
        srv.stop()
    return report


def _serve_until_idle(srv: Any, idle_s: float) -> None:
    """Keep a browser session alive while it is being used, and stop a forgotten one.

    `idle_s` is the gap after the last request. A first request starts the clock, so a URL nobody opens stops the process
    after the same gap rather than waiting for a page that will never load.
    """
    last = time.monotonic()
    seen = 0
    while True:
        time.sleep(0.05)
        if srv.requests != seen:
            seen = srv.requests
            last = time.monotonic()
        if time.monotonic() - last > max(0.2, float(idle_s)):
            return


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="blackbox desktop",
                                 description="Open the offline viewer in a desktop window (loopback, no network).")
    ap.add_argument("path", nargs="?", default=None,
                    help="a .bbx capsule or a data directory of them (default: the standard data directory)")
    ap.add_argument("--port", type=int, default=0, help="loopback port to bind (0 = choose one)")
    ap.add_argument("--selftest", action="store_true", help="report whether this machine can open a window, then exit")
    ap.add_argument("--no-window", action="store_true",
                    help="serve and print the URL instead of opening a window (the browser fallback, said out loud)")
    ap.add_argument("--onboarding", action="store_true",
                    help="open on the setup page even when recordings exist")
    ap.add_argument("--idle", type=float, default=900.0,
                    help="with --no-window: stop after this many seconds without a request (default 900)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        report = selftest()
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"blackbox {report['blackbox']} on {report['platform']}/{report['machine']}")
            print(f"window backend: {report['window_backend'] or 'none'}"
                  + ("" if report["gtk_ready"] is None else f" (GTK/WebKit ready: {report['gtk_ready']})"))
            if report["available"]:
                print("a desktop window can be opened on this machine")
            else:
                print(f"missing: {', '.join(report['missing'])}")
                print(f"install:  {report['install_hint']}")
        return 0 if report["available"] else 3

    path = args.path
    if path is None:
        from blackbox.cli.paths import default_data_dir
        path = default_data_dir()
    if not os.path.exists(path):
        message = f"{path} does not exist"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        else:
            print(f"blackbox desktop: {message}", file=sys.stderr)
        return 2
    if not args.no_window:
        report = selftest()
        if not report["available"]:
            message = (f"no window can be opened here: missing {', '.join(report['missing'])}. "
                       f"Install with: {report['install_hint']}")
            if args.json:
                print(json.dumps({"ok": False, "error": message, **report}, indent=2, sort_keys=True))
            else:
                print(f"blackbox desktop: {message}", file=sys.stderr)
                print("the browser fallback is `blackbox serve <path>`; this command will not pretend to be a desktop "
                      "app by opening a tab", file=sys.stderr)
            return 3
    def announce(url: str) -> None:
        # Printed before the wait, and flushed: this line is the only way a user learns the URL, and a buffered one would
        # arrive after the server had already stopped (the failure `blackbox serve` was fixed for).
        if args.json:
            print(json.dumps({"serving": url}), flush=True)
        else:
            print(f"serving on {url}  (this URL is a one-time secret; Ctrl-C to stop)", flush=True)

    try:
        out = run(path, port=args.port, window=not args.no_window, as_json=args.json,
                  announce=None if args.no_window is False else announce, timeout_s=args.idle,
                  onboarding=args.onboarding)
    except Exception as exc:                              # a page that cannot be built is a capsule that cannot be read
        message = f"{type(exc).__name__}: {exc}"
        if args.json:
            print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        else:
            print(f"blackbox desktop: {message}", file=sys.stderr)
        return 2
    if args.json:
        # One line per event in a mode that prints a stream: the announcement and the report are both single JSON
        # objects, so a caller can read them line by line (an indented object spans lines and breaks that).
        print(json.dumps({"ok": True, **out}, sort_keys=True) if args.no_window
              else json.dumps({"ok": True, **out}, indent=2, sort_keys=True))
    else:
        print(f"opened the library: {out['capsules']} capsule(s), {out['runs']} run(s), {out['events']} event(s), "
              f"{out['attention']} needing attention; window closed after {out['seconds']:g}s, server stopped")
    return 0
