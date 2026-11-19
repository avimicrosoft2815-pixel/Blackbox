"""The incident library and the first-run page, without a window in the way.

Two kinds of test live here and they check different things:

* **the model** — `ui/library.py` and `ui/onboarding.py` are ordinary functions over a directory and an environment, so
  they are tested as such: a real capsule, a corrupt one, a hundred of them, a planted secret in the environment.
* **the pages** — `ui/app.py` serves them on loopback; the tests fetch them the way a window would, assert the loopback
  bind and the token, and check that a page with no run in it says so instead of drawing an empty table.

Anything about pixels, scrolling or a real webview is not here: there is no display in this environment, and
`docs/desktop.md` says which rows are unverified because of it.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_determinism import _capsule_bytes, _record

from blackbox.cap import format as cap
from blackbox.ui import app as app_mod
from blackbox.ui import library, onboarding


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    uuid_hex = _record(data)
    _capsule_bytes(data, uuid_hex)
    return data


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ── the library model ───────────────────────────────────────────────────────
def test_a_recorded_capsule_is_one_row_with_what_happened(data_dir: Path) -> None:
    found = library.scan(str(data_dir))
    assert found.total == 1 and found.counts["capsules"] == 1
    row = found.rows[0]
    assert row.readable is True
    assert row.capsule_id and row.run_id and row.name.endswith(".bbx")
    assert row.events and row.events > 0, row
    assert row.status, row
    assert row.model_calls and row.model_calls > 0, "the recorded run made model calls; the row must say so"
    assert "no model call was recorded" not in row.attention
    assert row.bytes and row.bytes > 0


def test_a_broken_capsule_is_a_row_with_a_reason_not_a_crash(data_dir: Path) -> None:
    (data_dir / "capsules" / "broken.bbx").write_bytes(b"not a zip at all")
    found = library.scan(str(data_dir))
    assert found.total == 2
    broken = next(row for row in found.rows if row.name == "broken.bbx")
    assert broken.readable is False
    assert broken.attention and broken.attention[0].startswith("unreadable:")
    # The refusal code appears once: `str(exception)` already carries it, and prefixing it again read
    # "unreadable: BBX-E402: BBX-E402: …" in the row.
    assert broken.attention[0].count("BBX-E402") == 1, broken.attention[0]


def test_the_cache_serves_a_second_scan_without_re_reading(data_dir: Path) -> None:
    library.forget()
    first = library.scan(str(data_dir))
    second = library.scan(str(data_dir))
    assert {row.path for row in first.rows} == {row.path for row in second.rows}
    # A file replaced in place gets a new key (mtime and size), so the cache cannot serve a stale row.
    path = first.rows[0].path
    os.utime(path, (1, 1))
    library.forget(path)
    assert library.scan(str(data_dir)).total == 1


def test_search_matches_the_fields_a_person_types(data_dir: Path) -> None:
    found = library.scan(str(data_dir))
    row = found.rows[0]
    for query in (row.run_id or "", row.capsule_id or "", row.status or "", row.name.split(".")[0]):
        if not query:
            continue
        assert library.scan(str(data_dir), query=query).total >= 1, query
    assert library.scan(str(data_dir), query="nothing-matches-this").total == 0


def test_pagination_slices_without_losing_the_total(data_dir: Path) -> None:
    # Twenty capsules: copies of a real one, each with its own name. The library reads manifests only, so this is cheap
    # and it is the path a user with a hundred runs takes.
    source = next((data_dir / "capsules").glob("*.bbx"))
    for index in range(19):
        shutil.copyfile(source, data_dir / "capsules" / f"copy-{index:02d}.bbx")
    library.forget()
    first = library.scan(str(data_dir), limit=5)
    second = library.scan(str(data_dir), limit=5, offset=5)
    assert first.total == 20 and len(first.rows) == 5 and first.pages == 4
    assert len(second.rows) == 5 and {r.path for r in first.rows}.isdisjoint({r.path for r in second.rows})
    assert first.counts["capsules"] == 20


def test_sorting_is_stable_and_total_order_independent(data_dir: Path) -> None:
    source = next((data_dir / "capsules").glob("*.bbx"))
    for index in range(4):
        shutil.copyfile(source, data_dir / "capsules" / f"copy-{index}.bbx")
    library.forget()
    newest = library.scan(str(data_dir), sort="newest", limit=10).rows
    largest = library.scan(str(data_dir), sort="largest", limit=10).rows
    assert newest[0].mtime >= newest[-1].mtime
    assert (largest[0].bytes or 0) >= (largest[-1].bytes or 0)


def test_a_name_from_a_url_is_matched_not_joined(data_dir: Path) -> None:
    row = library.scan(str(data_dir)).rows[0]
    assert library.find_by_name(str(data_dir), row.name) == row.path
    for hostile in ("../capsules/x.bbx", "..", "/etc/passwd", "x/../../x", "", "c_/x.bbx"):
        assert library.find_by_name(str(data_dir), hostile) is None, hostile


def test_two_copies_of_one_capsule_are_two_rows_each_with_its_own_link(data_dir: Path) -> None:
    """A copied capsule keeps its id. Linking rows by id would open the first file for both and hide the duplicate."""
    source = next((data_dir / "capsules").glob("*.bbx"))
    shutil.copyfile(source, data_dir / "capsules" / "copy.bbx")
    library.forget()
    rows = library.scan(str(data_dir), limit=10).rows
    assert len(rows) == 2 and len({row.name for row in rows}) == 2
    assert len({row.capsule_id for row in rows}) == 1, "the two files really do carry one capsule id"
    paths = {library.find_by_name(str(data_dir), row.name) for row in rows}
    assert paths == {row.path for row in rows} and len(paths) == 2


def test_the_manifest_only_reader_agrees_with_the_full_one(data_dir: Path) -> None:
    """The library's fast path must refuse what the full reader refuses, or it is a second weaker reader."""
    path = next((data_dir / "capsules").glob("*.bbx"))
    fast = cap.read_manifest(str(path))
    full = cap.read_capsule(str(path))
    assert fast["capsule_id"] == full.manifest["capsule_id"]
    assert fast["counts"] == full.manifest["counts"]


# ── onboarding ─────────────────────────────────────────────────────────────
def test_onboarding_reports_the_runtime_the_dir_and_the_recording(data_dir: Path) -> None:
    report = onboarding.probe(str(data_dir), env={}, find_spec=lambda name: None)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["python"]["severity"] == "ok"
    assert checks["data_dir"]["severity"] == "ok" and str(data_dir) in checks["data_dir"]["detail"]
    assert checks["runs"]["severity"] == "ok"
    assert checks["sdk:openai"]["severity"] == "action"
    assert "pip install openai" in checks["sdk:openai"]["command"]
    assert report["captured"] and report["not_captured"]


def test_onboarding_never_reads_or_prints_a_secret(tmp_path: Path) -> None:
    """The check looks for the variable's presence; the planted value must not appear in the report or the page."""
    planted = "sk-planted-do-not-print-0123456789"
    data = tmp_path / "empty"
    data.mkdir()
    report = onboarding.probe(str(data), env={"OPENAI_API_KEY": planted}, find_spec=lambda name: object())
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["provider:openai"]["severity"] == "ok"
    assert "OPENAI_API_KEY" in checks["provider:openai"]["detail"]
    rendered = json.dumps(report)
    assert planted not in rendered, "the onboarding report leaked a key value"

    srv = app_mod.start(str(data), force_onboarding=True)
    try:
        status, page = _get(srv.url + "onboarding")
    finally:
        srv.stop()
    assert status == 200
    assert planted not in page


def test_an_empty_directory_is_onboarded_not_an_empty_table(tmp_path: Path) -> None:
    data = tmp_path / "nothing"
    data.mkdir()
    report = onboarding.probe(str(data), env={}, find_spec=lambda name: None)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["runs"]["severity"] == "action"
    assert "blackbox run" in checks["runs"]["command"]
    assert report["state"] in ("ready", "blocked")


# ── the pages, over loopback ───────────────────────────────────────────────
def test_the_app_serves_the_library_and_a_run_and_refuses_a_wrong_token(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _get(srv.url)
        assert status == 200
        assert "Find a run" in page and "c_" in page
        assert f'{srv.token}/onboarding' in page

        name = library.scan(str(data_dir)).rows[0].name
        status, run = _get(srv.url + f"run/{name}")
        assert status == 200 and "<!doctype html>" in run.lower()
        assert "recorded runs" in run, "a run page must offer a way back to the library"

        status, _ = _get(srv.url.replace(srv.token, "wrong-token"))
        assert status == 404
        status, _ = _get(srv.url + "run/../etc/passwd")
        assert status in (404, 400)
    finally:
        srv.stop()


def test_the_first_page_is_onboarding_when_nothing_is_recorded(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    data.mkdir()
    srv = app_mod.start(str(data))
    try:
        status, page = _get(srv.url)
    finally:
        srv.stop()
    assert status == 200
    assert "first run" in page and "What is recorded" in page
    # Onboarding instead of the library: no run table and no tiles on a machine with nothing recorded.
    assert "Find a run" not in page
    assert '<section class="tiles"' not in page


def test_a_local_only_page_cannot_reach_anything(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    data.mkdir()
    srv = app_mod.start(str(data))
    try:
        with urllib.request.urlopen(srv.url, timeout=15) as response:
            csp = response.headers.get("Content-Security-Policy", "")
            body = response.read().decode()
    finally:
        srv.stop()
    assert "default-src 'none'" in csp, csp
    assert "Referrer-Policy" and response.headers.get("Referrer-Policy") == "no-referrer"
    # No reference out of the page: no scheme-qualified URL, no protocol-relative host, no @import.
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", body), "the page references an external URL"
    assert not re.search(r"@import\s+(?:url|['\"])", body), "the page imports a stylesheet"


def test_the_server_binds_loopback_only_and_is_not_logged(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        assert srv.url.startswith("http://127.0.0.1:")
        address = srv._httpd.server_address[0]
        assert address == "127.0.0.1"
        # The token is in the path, so the handler's log method must do nothing at all: read the function and check it.
        body = inspect.getsource(app_mod._Handler.log_message)
        assert [line.strip() for line in body.splitlines()[1:] if line.strip()] == ["return"]
    finally:
        srv.stop()


def test_every_page_a_user_needs_is_under_the_token(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        for route in ("", "library", "onboarding"):
            status, page = _get(srv.url + route)
            assert status == 200, route
            assert srv.token in page, route
    finally:
        srv.stop()


def test_the_app_server_cannot_connect_outward_or_bind_a_wildcard() -> None:
    """`ui/app.py` is the second loopback server; the first one's scope checks apply to it too, on the syntax tree."""
    import ast

    source = (ROOT / "src" / "blackbox" / "ui" / "app.py").read_text(encoding="utf-8")
    assert ".".join(["0"] * 4) not in source, "a wildcard bind would reach the network"
    assert "127.0.0.1" not in source, "the address comes from ui.server, so there is one place to check it"
    banned = {"connect", "connect_ex", "create_connection", "urlopen", "sendto", "socket"}
    hits = [node.attr for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr in banned]
    assert not hits, f"an outward-capable call in the page server: {hits}"


def test_every_static_asset_travels_with_the_package() -> None:
    """A page is built from files next to the module; a wheel that ships the code and not the template 500s after install.

    `pyproject.toml` declares `blackbox.ui = ["static/*.html", "static/*.css", "static/*.js"]`. This checks the two ways
    that goes wrong without a build: a new asset with an extension those globs do not match, and a page name the code
    reads that is not on disk.
    """
    import re

    static = ROOT / "src" / "blackbox" / "ui" / "static"
    declared = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    globs = re.findall(r'"(static/\*\.\w+)"', declared)
    assert globs, "the package-data line for blackbox.ui has moved; this test needs its new spelling"
    for asset in sorted(static.iterdir()):
        assert f"static/*{asset.suffix}" in globs, f"{asset.name} is not matched by any package-data glob"
    for source in sorted((ROOT / "src" / "blackbox" / "ui").glob("*.py")):
        for name in re.findall(r'_static\("([^"]+)"\)', source.read_text(encoding="utf-8")):
            assert (static / name).is_file(), f"{source.name} reads static/{name}, which does not exist"
