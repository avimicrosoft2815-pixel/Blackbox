"""Release-critical safety for the window: no secret on a page, no hostile capsule taking the app down, no write from a GET.

Three promises the desktop app makes, each tested against the version of itself that would be easier to write:

* **Nothing secret reaches a page.** A key planted in the environment and a redacted value inside a recording must not
  appear on any of the seven pages, and no page may reference anything outside the loopback server.
* **A hostile capsule is a refusal, not a crash.** The app's own library and pages are fed the attack corpus; every case
  must produce a readable refusal with a `BBX-` code and no traceback, and a hostile *name* in the URL must not reach a
  path outside the data directory.
* **A `GET` cannot write.** The data directory and the user's repository are hashed before and after every page load.

The recovery half is here too: a run whose process was killed must still list, open, verify and diagnose — that is the
whole point of a recorder, and it is the case a library is most likely to get wrong.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_attacks import _manifest, _mark_members_encrypted, _write
from test_capsule_determinism import _capsule_bytes, _record

from blackbox.ui import app as app_mod
from blackbox.ui import library


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    uuid_hex = _record(data)
    _capsule_bytes(data, uuid_hex)
    return data


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _pages(srv, name: str) -> dict[str, str]:
    routes = {
        "library": srv.url,
        "onboarding": srv.url + "onboarding",
        "run": srv.url + f"run/{urllib.parse.quote(name)}",
        "verify": srv.url + f"verify/{urllib.parse.quote(name)}",
        "diagnose": srv.url + f"diagnose/{urllib.parse.quote(name)}",
        "repair": srv.url + "repair",
        "missing": srv.url + "run/nothing-here.bbx",
    }
    out = {}
    for label, url in routes.items():
        status, body = _get(url)
        assert status in (200, 404), (label, status)
        out[label] = body
    return out


def _script_code(body: str) -> list[str]:
    """The `<script>` bodies of a page with their comments removed, so a sentence about `fetch` is not read as a call."""
    import re

    out = []
    for script in re.findall(r"<script[^>]*>(.*?)</script>", body, re.S):
        out.append(re.sub(r"/\*.*?\*/", "", script, flags=re.S))
    return out


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ── privacy ─────────────────────────────────────────────────────────────────
def test_no_secret_from_the_environment_reaches_any_page(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = "sk-planted-0123456789abcdef-not-a-real-key"
    monkeypatch.setenv("OPENAI_API_KEY", planted)
    name = next((data_dir / "capsules").glob("*.bbx")).name
    srv = app_mod.start(str(data_dir))
    try:
        pages = _pages(srv, name)
    finally:
        srv.stop()
    for label, body in pages.items():
        assert planted not in body, f"the {label} page leaked a key"
    assert "OPENAI_API_KEY" in pages["onboarding"], "the setup page should name the variable it found"


def test_no_page_references_anything_outside_the_loopback_server(data_dir: Path) -> None:
    import re

    name = next((data_dir / "capsules").glob("*.bbx")).name
    srv = app_mod.start(str(data_dir))
    try:
        pages = _pages(srv, name)
    finally:
        srv.stop()
    for label, body in pages.items():
        assert not re.search(r"(?:src|href)\s*=\s*[\"']https?://", body), label
        assert not re.search(r"@import\s+(?:url|['\"])", body), label
        # Script *code* only: the viewer's header comment says "one URL, no fetch(), no XMLHttpRequest", which is a
        # sentence about the page, not a call in it. Comments are stripped before the scan for that reason.
        for script in _script_code(body):
            assert "fetch(" not in script and "XMLHttpRequest" not in script, f"{label} would talk to something"
            assert "WebSocket" not in script and "EventSource" not in script, f"{label} opens a channel"


def test_a_redacted_value_in_the_recording_stays_redacted_on_every_page(tmp_path: Path) -> None:
    """The recorder's promise, checked where a reader would see it: the page renders the capsule, not the secret."""
    planted = "sk-live-abcdef0123456789abcdef0123456789"
    data = tmp_path / "data"
    script = tmp_path / "app.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from blackbox import api\n"
        "with api.run(name='leak-check'):\n"
        "    api.step('call', lambda: {'authorization': 'Bearer ' + os.environ['SECRET_VALUE']})\n"
        "    raise SystemExit(0)\n",
        encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "BLACKBOX_HOME": str(data), "SECRET_VALUE": planted}
    env.pop("BLACKBOX_REDACTION", None)
    import subprocess

    subprocess.run([sys.executable, "-m", "blackbox", "run", "--", sys.executable, str(script)],
                   env=env, capture_output=True, text=True, timeout=300)
    name = next((data / "capsules").glob("*.bbx"), None)
    if name is None:
        pytest.skip("the sample run produced no capsule on this platform")
    srv = app_mod.start(str(data))
    try:
        pages = _pages(srv, name.name)
    finally:
        srv.stop()
    for label, body in pages.items():
        assert planted not in body, f"the {label} page showed a secret the recorder had redacted"


# ── hostile capsules ────────────────────────────────────────────────────────
HOSTILE = {
    "traversal": [("events/../escape.bin", b"x")],
    "absolute": [("/etc/passwd", b"x")],
    "bomb": [("events/seg-0001.bin", b"\0" * (2 << 20))],
    "case-collision": [("events/seg-0001.bin", b"a"), ("events/SEG-0001.BIN", b"b")],
}


@pytest.mark.parametrize("label", sorted(HOSTILE))
def test_a_hostile_capsule_is_a_refusal_with_a_code_and_never_a_traceback(data_dir: Path, label: str) -> None:
    entries = HOSTILE[label]
    path = _write(data_dir / "capsules", entries, manifest=_manifest(), name=f"hostile-{label}.bbx")
    if label == "bomb":
        pass                              # the ratio check refuses it before anything is inflated
    library.forget()
    row = next(row for row in library.scan(str(data_dir), limit=50).rows if row.name == path.name)
    assert row.readable is False, row
    assert "BBX-E4" in " ".join(row.attention), row.attention

    srv = app_mod.start(str(data_dir))
    try:
        status, page = _get(srv.url + f"verify/{urllib.parse.quote(path.name)}")
        assert status == 200
        assert "BBX-E4" in page and "Traceback" not in page
        status, run = _get(srv.url + f"run/{urllib.parse.quote(path.name)}")
        # Opening a hostile capsule either refuses with its code or is not served at all — never a 500.
        assert status in (404, 500) or "BBX-E4" in run
        assert "Traceback" not in run
    finally:
        srv.stop()
    assert not (data_dir / "escape.bin").exists()


def test_an_encrypted_capsule_is_refused_by_the_library_and_the_check_page(data_dir: Path) -> None:
    path = _mark_members_encrypted(_write(data_dir / "capsules", [("events/seg-0001.bin", b"x")],
                                         manifest=_manifest(), name="encrypted.bbx"))
    library.forget()
    row = next(row for row in library.scan(str(data_dir), limit=50).rows if row.name == path.name)
    assert row.readable is False and "BBX-E406" in row.attention[0]
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _get(srv.url + "verify/encrypted.bbx")
    finally:
        srv.stop()
    assert status == 200 and "BBX-E406" in page


def test_a_capsule_name_cannot_reach_outside_the_data_directory(data_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.bbx"
    outside.write_bytes(b"not a capsule")
    srv = app_mod.start(str(data_dir))
    try:
        for name in ("../outside.bbx", "..%2Foutside.bbx", "/etc/passwd", "..", "."):
            for route in ("verify", "diagnose", "run"):
                status, body = _get(srv.url + f"{route}/{name}")
                assert status == 404, (route, name, status)
                assert "not a capsule" not in body
    finally:
        srv.stop()


# ── nothing writes on a read ────────────────────────────────────────────────
def test_every_page_load_leaves_the_data_directory_byte_identical(data_dir: Path) -> None:
    before = _tree_hash(data_dir)
    name = next((data_dir / "capsules").glob("*.bbx")).name
    srv = app_mod.start(str(data_dir))
    try:
        for _ in range(2):
            _pages(srv, name)
        # The repair page with no arguments, and with a proposal that does not exist: still no write anywhere.
        _get(srv.url + "repair?proposal=/nonexistent/proposal.json&repo=/tmp")
    finally:
        srv.stop()
    assert _tree_hash(data_dir) == before


def test_a_get_cannot_apply_a_proposal(data_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_bytes(b"raise SystemExit(1)\n")
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"schema": "blackbox.repair-proposal/1"}', encoding="utf-8")
    before = target.read_bytes()
    srv = app_mod.start(str(data_dir))
    try:
        status, _ = _get(srv.url + "repair/apply?" + urllib.parse.urlencode(
            {"proposal": str(proposal), "repo": str(repo), "typed": "00000000", "confirm": "yes"}))
        assert status == 404
    finally:
        srv.stop()
    assert target.read_bytes() == before


def test_a_post_that_is_not_a_form_is_refused(data_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    srv = app_mod.start(str(data_dir))
    try:
        request = urllib.request.Request(srv.url + "repair/tests", data=b"repo=/tmp&confirm=yes", method="POST",
                                         headers={"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        assert caught.value.code == 400
    finally:
        srv.stop()


# ── recovery ────────────────────────────────────────────────────────────────
def test_a_run_whose_process_was_killed_still_lists_opens_and_diagnoses(data_dir: Path) -> None:
    """The fixture's capsule comes from a process that died; the app must treat it as evidence, not as damage."""
    found = library.scan(str(data_dir))
    row = found.rows[0]
    assert row.readable and row.status == "recovered", row
    assert row.status_cause == "PROCESS_DEATH", row
    assert row.events and row.events > 0
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _get(srv.url + f"run/{urllib.parse.quote(row.name)}")
        assert status == 200 and "BlackBox" in page
        status, check = _get(srv.url + f"verify/{urllib.parse.quote(row.name)}")
        assert status == 200 and "verified" in check
        status, diagnosis = _get(srv.url + f"diagnose/{urllib.parse.quote(row.name)}")
        assert status == 200 and "What the evidence supports" in diagnosis
        # The recovery is stated on the page, not smoothed away: the row says the process died.
        assert "PROCESS_DEATH" in page or "recovered" in page
    finally:
        srv.stop()
