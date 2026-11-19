"""The desktop window: what it does, what it refuses to do, and what it leaves behind.

There is no display in this environment and no GTK/WebKit, so the shell is tested the way a shell should be: with a
**fake webview module** that records the window it was asked for and fetches the URL while it pretends to be open. That
exercises the real wiring — the loopback server, the secret path, the page, the stop-after-close — without pretending a
window was seen. `blackbox desktop --selftest` on a machine with the stack installed is the other half of the evidence,
and `docs/desktop.md` says which machines that has happened on.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_determinism import _capsule_bytes, _record

from blackbox.ui import desktop


class FakeWebview(types.ModuleType):
    """The smallest thing that behaves like `webview` for this test: a window, a blocking `start`, a fetch."""

    def __init__(self, *, fetch: bool = True, statuses: list[int] | None = None) -> None:
        super().__init__("webview")
        self.windows: list[dict] = []
        self.start_kwargs: dict = {}
        self.fetch = fetch
        self.statuses = statuses if statuses is not None else []
        self.body = b""

    def create_window(self, title, url, **kwargs):         # mirrors pywebview's signature
        window = {"title": title, "url": url, **kwargs}
        self.windows.append(window)
        return window

    def start(self, **kwargs):                              # mirrors pywebview's signature
        self.start_kwargs = kwargs
        if self.fetch and self.windows:
            url = self.windows[-1]["url"]
            with urllib.request.urlopen(url, timeout=10) as response:
                self.statuses.append(response.status)
                self.body = response.read()


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    _capsule_bytes(data_dir, uuid_hex)
    return next(data_dir.glob("capsules/*.bbx"))


def _port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# ── what the shell wires together ────────────────────────────────────────────
def test_a_window_opens_on_loopback_and_serves_the_real_page(capsule: Path) -> None:
    fake = FakeWebview()
    report = desktop.run(str(capsule), webview_module=fake)
    assert len(fake.windows) == 1, fake.windows
    url = fake.windows[0]["url"]
    assert url.startswith("http://127.0.0.1:"), url
    assert fake.statuses == [200], "the window fetched the page and got something else"
    assert b"BlackBox" in fake.body and len(fake.body) == report["bytes"]
    assert report["served"] >= 1
    assert report["capsule"] and report["events"] > 0


def test_the_server_stops_when_the_window_closes(capsule: Path) -> None:
    fake = FakeWebview()
    report = desktop.run(str(capsule), webview_module=fake)
    port = int(fake.windows[0]["url"].split(":")[2].split("/")[0])
    assert _port_closed(port), f"a listener on {port} outlived the window"
    assert report["url"].endswith("/")


def test_the_window_is_not_a_debug_console(capsule: Path) -> None:
    """`debug=True` would let a keystroke open an inspector onto the page — and the page holds a capsule."""
    fake = FakeWebview()
    desktop.run(str(capsule), webview_module=fake)
    assert fake.start_kwargs.get("debug") is False
    assert fake.start_kwargs.get("private_mode") is True
    window = fake.windows[0]
    assert window["width"] >= 1024 and window["height"] >= 700, window
    assert window["min_size"] == desktop.MIN_SIZE
    assert "BlackBox" in window["title"]


def test_the_url_is_a_secret_path_not_a_query_string(capsule: Path) -> None:
    fake = FakeWebview()
    report = desktop.run(str(capsule), webview_module=fake)
    path = report["url"].split("://", 1)[1].split("/", 1)[1]
    assert len(path.strip("/")) >= 24, "the token is short enough to guess"
    assert "?" not in report["url"], "a token in a query string reaches logs and Referer headers"


def test_a_directory_of_capsules_opens_as_a_run_list(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    uuid_hex = _record(data_dir)
    _capsule_bytes(data_dir, uuid_hex)
    fake = FakeWebview()
    report = desktop.run(str(data_dir), webview_module=fake)
    assert report["capsules"] == 1
    assert fake.statuses == [200]


# ── what it refuses ─────────────────────────────────────────────────────────
def test_selftest_says_exactly_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_import_webview", lambda: None)
    report = desktop.selftest()
    assert report["available"] is False
    assert "pywebview" in report["missing"]
    assert report["window_backend"] is None
    assert "blackbox-recorder[desktop]" in report["install_hint"]


def test_selftest_reports_a_window_when_the_stack_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "_import_webview", lambda: FakeWebview())
    monkeypatch.setattr(desktop, "_gtk_available", lambda: True)
    report = desktop.selftest()
    assert report["available"] is True and report["missing"] == []
    assert report["window_backend"] == "pywebview"


def test_main_exits_three_and_names_the_browser_fallback(capsule: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """No window must never become a browser tab wearing the word "desktop"."""
    monkeypatch.setattr(desktop, "_import_webview", lambda: None)
    monkeypatch.setattr(desktop, "_gtk_available", lambda: False)
    code = desktop.main([str(capsule)])
    captured = capsys.readouterr()
    assert code == 3
    assert "missing" in captured.err and "blackbox serve" in captured.err
    assert captured.out == "", "a refusal must not print a URL and hope"


def test_main_reports_selftest_as_json(capsule: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(desktop, "_import_webview", lambda: FakeWebview())
    monkeypatch.setattr(desktop, "_gtk_available", lambda: True)
    assert desktop.main(["--selftest", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["available"] is True and report["platform"] in ("linux", "win32", "darwin")


def test_a_path_that_does_not_exist_is_exit_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert desktop.main([str(tmp_path / "nope.bbx")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_browser_fallback_announces_the_url_before_it_waits(capsule: Path) -> None:
    """`--no-window` serves one fetch and waits for it, like `blackbox serve` — it is documented, not a quiet default.

    The first version of `run()` waited for a fetch and left announcing the URL to the caller, so the caller could only
    print the URL after the wait had ended: a fallback nobody could reach. `announce` is called before the wait, and this
    test fetches the page from inside that callback, which is exactly the user's timeline.
    """
    seen: dict[str, str] = {}

    def announce(url: str) -> None:
        seen["url"] = url
        threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()

    report = desktop.run(str(capsule), window=False, announce=announce, timeout_s=30)
    assert seen.get("url", "").startswith("http://127.0.0.1:")
    assert report["window"] is False and report["served"] == 1, report


def test_main_prints_the_fallback_url_as_json_before_waiting(capsule: Path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    real_run = desktop.run

    def run_and_fetch(*args, **kwargs):
        def announce(url: str) -> None:
            kwargs["announce"](url)
            threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()
            time.sleep(0.2)                                # let the fetch land before the wait starts
        return real_run(*args, **{**kwargs, "announce": announce})

    monkeypatch.setattr(desktop, "run", run_and_fetch)
    assert desktop.main([str(capsule), "--no-window", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "serving" in lines[0], lines
    assert lines[0]["serving"].startswith("http://127.0.0.1:")
    assert lines[-1]["ok"] is True and lines[-1]["window"] is False


# ── it is a real command, next to the others ────────────────────────────────
def test_desktop_is_on_the_front_door_with_json_and_a_documented_exit() -> None:
    from blackbox import __main__ as front_door

    assert "desktop" in front_door.KNOWN
    assert front_door.COMMANDS["desktop"] == ("blackbox.ui.desktop", "main")
    import subprocess

    help_text = subprocess.run([sys.executable, "-m", "blackbox", "desktop", "--help"], capture_output=True,
                               text=True, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
                               timeout=120).stdout
    assert "--json" in help_text and "--selftest" in help_text and "--no-window" in help_text
