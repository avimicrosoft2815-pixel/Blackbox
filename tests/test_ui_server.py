"""P4-11 — `blackbox serve`: the viewer over loopback, once, at an unguessable path.

The security claims of this ticket are small and specific, so they are the tests:

* **loopback only.** The bind tuple is a literal `("127.0.0.1", port)`; there is no host parameter and no wildcard
  address anywhere in the module. Asserted on the source (AST/literal search), because the risk is a future edit, not
  this version.
* **it never connects out.** A server that fetches would make the loopback bind meaningless. The module has no
  `connect`, `create_connection`, `urlopen` or `socket.socket(...)` — checked on the syntax tree.
* **the token is a path secret, required on every request, and one-shot.** A wrong path is a 404 that does not confirm
  anything; the right one is served once and the socket closes.
* **the response carries the headers that make an inlined page safe**: `CSP` with `default-src 'none'`, `nosniff`,
  `no-referrer`, `no-store`.
* **nothing is logged.** The request line contains the token, so a handler that logs it leaks the capsule it serves.

The one honest deviation from the plan's P4-11 row — "one-time token in fragment" — is recorded in
`docs/DEVIATIONS.md`: a fragment never reaches the server, so a server cannot require one, and this page needs no
second request to validate anything because the capsule is inlined.
"""
from __future__ import annotations

import ast
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.ui import server, viewer

SOURCE = (ROOT / "src" / "blackbox" / "ui" / "server.py").read_text(encoding="utf-8")
WILDCARD = ".".join(["0"] * 4)


@pytest.fixture(scope="module")
def page() -> str:
    return viewer.build_html({"viewer": {"run_id": "r_serve", "capsule_path": "c.bbx", "role": "", "writer": "",
                                        "problems": [], "root_ok": True},
                              "run": {"run_id": "r_serve", "capsule_run_id": "r_c", "status": "recovered",
                                      "status_cause": None, "completeness": "full", "truncated_tail": False,
                                      "lost_from_seq": None, "durability_class": "F1", "started_ns": 0,
                                      "ended_ns": 1},
                              "capture": {}, "redaction": {"content_mode": "full"}, "gaps": [], "links": {"links": []},
                              "events": [], "nodes": [], "tapes": [], "cost": {}, "counts": {},
                              "diagnosis": {"error": "no frames"}})


@pytest.fixture()
def served(page):
    srv = server.start(page, port=0)
    yield srv
    srv.stop()


# ── static: it can only be what it says it is ──────────────────────────────────────────────────────────────────────
def test_the_bind_is_a_loopback_literal_and_the_module_has_no_wildcard():
    """The address is a constant and the bind uses it; there is no third source for a host anywhere in the file."""
    assert 'HOST = "127.0.0.1"' in SOURCE
    assert WILDCARD not in SOURCE
    assert "_Viewer((HOST, port)" in SOURCE, "the only bind must be built from the loopback constant"
    assert SOURCE.count("super().__init__(address") == 1, "the server takes its address from its constructor"
    assert "sys.argv" not in SOURCE and "environ" not in SOURCE, "and the address cannot come from outside the module"


def test_the_server_module_cannot_connect_outward():
    tree = ast.parse(SOURCE)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"connect", "connect_ex", "create_connection", "urlopen",
                                                             "open_connection", "getaddrinfo"}:
            offenders.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "socket":
            offenders.append(f"line {node.lineno}: socket()")
    assert not offenders, f"the viewer server must never be a client: {offenders}"


def test_the_exemption_is_named_in_the_policy_file():
    """The exemption list is the review surface: a new network-capable file has to appear in it."""
    sys.path.insert(0, str(ROOT / "tests"))
    import _capture_policy

    assert "ui/server.py" in _capture_policy.SERVERS
    assert frozenset({"http", "socket", "socketserver"}) == _capture_policy.SERVER_MODULES


# ── the transaction ────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_page_arrives_with_the_headers_that_keep_an_inlined_page_safe(served):
    with urllib.request.urlopen(served.url, timeout=5) as response:
        body = response.read()
        assert response.status == 200
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert "connect-src" not in csp, "with default-src 'none' there is nothing to connect to, and saying it twice invites drift"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Type"].startswith("text/html")
    assert body.startswith(b"<!doctype html>")


def test_a_wrong_path_is_404_and_confirms_nothing(served):
    for path in ("/", "/index.html", "/" + "x" * 32 + "/"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen("http://" + served.endpoint + path, timeout=5)
        assert caught.value.code == 404
        assert served.token.encode() not in caught.value.read()
    assert served.served == 0, "a 404 must not consume the one-shot token"


def _settle(srv, count: int = 1, spins: int = 200) -> None:
    """The counter is incremented by the handler thread; wait for it instead of racing the socket."""
    import time

    for _ in range(spins):
        if srv.served >= count:
            return
        time.sleep(0.01)
    raise AssertionError(f"expected {count} served page(s), saw {srv.served}")


def test_a_favicon_request_does_not_consume_the_token(served):
    with urllib.request.urlopen("http://" + served.endpoint + "/favicon.ico", timeout=5) as response:
        assert response.status == 204
    assert served.served == 0
    with urllib.request.urlopen(served.url, timeout=5) as response:
        assert response.status == 200
    _settle(served, 1)


def test_the_page_is_served_once(served):
    with urllib.request.urlopen(served.url, timeout=5) as response:
        assert response.status == 200
    _settle(served, 1)
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(served.url, timeout=5)
    assert caught.value.code == 404, "the second request is refused by the handler, not by a timing race"
    assert served.served == 1


def test_the_token_is_long_and_never_appears_in_the_body(served):
    assert len(served.token) >= 32
    assert served.token in served.url
    with urllib.request.urlopen(served.url, timeout=5) as response:
        assert served.token.encode() not in response.read()


def test_keep_open_serves_more_than_once(page):
    srv = server.start(page, port=0, one_shot=False)
    try:
        for expected in (1, 2):
            with urllib.request.urlopen(srv.url, timeout=5) as response:
                assert response.status == 200
            _settle(srv, expected)
    finally:
        srv.stop()


def test_nothing_is_logged(served, capfd):
    """The request line carries the token; a handler that logs it writes the secret to stderr and into CI logs."""
    with urllib.request.urlopen(served.url, timeout=5) as response:
        response.read()
    captured = capfd.readouterr()
    assert served.token not in captured.out and served.token not in captured.err
    assert "GET /" not in captured.err, "the access log is silenced: a token in a log is a leaked capsule"


# ── the command line ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_cli_prints_the_url_and_exits_0_after_the_fetch(tmp_path, monkeypatch, capsys):
    """`serve` has to stop by itself once it has done its job; a viewer that lingers is a port left open."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_diagnose import _clean

    data = tmp_path / "data"
    (data / "capsules").mkdir(parents=True)
    (data / "capsules" / "c_x.bbx").write_bytes(Path(_clean(tmp_path)).read_bytes())

    calls: dict = {}
    real_start = server.start

    def fake_start(page, *, port=0, one_shot=True):
        srv = real_start(page, port=port, one_shot=one_shot)
        calls["srv"] = srv
        with urllib.request.urlopen(srv.url, timeout=5) as response:      # the "browser"
            calls["status"] = response.status
        return srv

    monkeypatch.setattr(server, "start", fake_start)
    assert server.main([str(data)]) == 0
    out = capsys.readouterr().out
    assert "serving" in out and "127.0.0.1" in out
    assert calls["status"] == 200
    assert calls["srv"].served == 1


def test_the_cli_refuses_a_path_it_cannot_render(tmp_path, capsys):
    assert server.main([str(tmp_path / "nothing-here")]) == 2
    assert "blackbox serve:" in capsys.readouterr().err


def test_the_cli_times_out_honestly_when_nobody_fetches(tmp_path, capsys, monkeypatch):
    import json

    data = tmp_path / "data"
    (data / "capsules").mkdir(parents=True)
    capsule = data / "capsules" / "c_x.bbx"
    sys.path.insert(0, str(ROOT / "tests"))
    from test_diagnose import _clean  # a real capsule, built the real way

    built = _clean(tmp_path)
    capsule.write_bytes(Path(built).read_bytes())
    assert json.dumps({"x": 1})
    code = server.main([str(data), "--timeout", "0.2"])
    out = capsys.readouterr()
    assert code == 1, "an unfetched page is a failure to report, not a success"
    assert "nothing fetched it" in out.err
    assert "127.0.0.1" in out.out and "secret" in out.out
