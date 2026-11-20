"""The window's four flows over HTTP: check, diagnose, review a repair, apply it after approval, verify the result.

The CLI already has every one of these; what this file tests is that the **window** keeps the same gates, because a button
is easier to press than a command is to type:

* a check writes nothing and reports the refusal code of a capsule that cannot be read;
* a diagnosis renders findings and the brief **as text**, so recorded content cannot become markup;
* a review shows the diff and the token, and writes nothing;
* an apply refuses without the ticked box, without a typed token, and with a **wrong** typed token — and the repository is
  byte-identical after each refusal;
* a correct typed token applies exactly once, leaves a backup, and a later apply of the same proposal refuses because the
  tree moved (the token no longer matches);
* a `POST` from another origin, or without the form token, is refused before any flow runs;
* the tests button runs what it shows, and refuses without the tick.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_capsule_determinism import _capsule_bytes, _record

from blackbox.ui import app as app_mod

SCHEMA = "blackbox.repair-proposal/1"


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    uuid_hex = _record(data)
    _capsule_bytes(data, uuid_hex)
    return data


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "src" / "app.py", "def handler():\n    raise RuntimeError('boom')\n")
    return repo


def _proposal(tmp_path: Path, repo: Path, *, content: str | None = None) -> Path:
    target = repo / "src" / "app.py"
    document = {
        "schema": SCHEMA,
        "title": "catch the error instead of raising",
        "rationale": "the tape shows a raise in this handler",
        "tests": [sys.executable + " -c \"print('ok')\""],
        "risk": ["changes control flow"],
        "changes": [{
            "path": "src/app.py",
            "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "content": content if content is not None else "def handler():\n    return 'caught'\n",
        }],
    }
    path = tmp_path / "proposal.json"
    path.write_bytes(json.dumps(document, indent=2).encode("utf-8"))
    return path


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _post(url: str, fields: dict[str, str], *, origin: str | None = None) -> tuple[int, str, str]:
    """POST one form and follow the 303 the way a browser does. Returns (status, body, location)."""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    if origin:
        request.add_header("Origin", origin)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.read().decode(), response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Location", "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(html_text: str) -> str:
    """The page as words, for assertions that are about what a person reads rather than about markup."""
    import re

    body = html_text[html_text.index("<body>"):]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ── check and diagnose ──────────────────────────────────────────────────────
def test_the_check_page_recomputes_the_index_and_says_what_it_found(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        name = next((data_dir / "capsules").glob("*.bbx")).name
        status, page = _get(srv.url + f"verify/{name}")
    finally:
        srv.stop()
    assert status == 200
    words = _text(page)
    assert "verified" in words and "integrity root" in words
    assert "entries" in words and "in " in words


def test_a_capsule_that_cannot_be_read_is_a_refusal_with_its_code(data_dir: Path) -> None:
    (data_dir / "capsules" / "broken.bbx").write_bytes(b"not a zip")
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _get(srv.url + "verify/broken.bbx")
    finally:
        srv.stop()
    assert status == 200
    words = _text(page)
    assert "cannot be checked" in words and "BBX-E402" in words


def test_the_diagnosis_page_shows_findings_and_the_brief_as_text(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        name = next((data_dir / "capsules").glob("*.bbx")).name
        status, page = _get(srv.url + f"diagnose/{name}")
    finally:
        srv.stop()
    assert status == 200
    words = _text(page)
    assert "What the evidence supports" in words
    assert "The repair brief" in words and "does not send it anywhere" in words
    # Verdicts are the brief's vocabulary, not a score.
    assert any(verdict in words for verdict in ("observed", "likely", "possible", "cannot-tell"))


def test_a_capsule_name_from_the_url_is_matched_not_joined(data_dir: Path) -> None:
    srv = app_mod.start(str(data_dir))
    try:
        for route in ("verify", "diagnose", "run"):
            status, _ = _get(srv.url + f"{route}/../etc/passwd")
            assert status == 404, route
    finally:
        srv.stop()


def test_the_brief_and_the_report_can_be_saved_as_files(data_dir: Path) -> None:
    """Export: the app hands the person a document; it does not put one on disk anywhere itself."""
    name = next((data_dir / "capsules").glob("*.bbx")).name
    srv = app_mod.start(str(data_dir))
    try:
        for kind, marker in (("brief", "repair brief"), ("report", "diagnosis")):
            with urllib.request.urlopen(srv.url + f"export/{name}/{kind}", timeout=30) as response:
                assert response.status == 200
                disposition = response.headers.get("Content-Disposition", "")
                body = response.read().decode("utf-8")
            assert "attachment" in disposition and kind in disposition.lower(), disposition
            assert body.strip(), f"the {kind} export was empty"
            assert marker.lower() in body.lower() or "blackbox" in body.lower(), body[:200]
        # An export of something that is not there is a 404, not an empty file.
        try:
            urllib.request.urlopen(srv.url + "export/nothing-here.bbx/brief", timeout=30)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("a missing capsule must not export an empty document")
        try:
            urllib.request.urlopen(srv.url + f"export/{name}/something-else", timeout=30)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("an unknown export kind must not export anything")
    finally:
        srv.stop()


# ── review, refuse, approve ─────────────────────────────────────────────────
def test_the_review_page_shows_the_diff_the_risk_and_the_token_and_writes_nothing(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    before = _sha(repo / "src" / "app.py")
    srv = app_mod.start(str(data_dir))
    try:
        query = urllib.parse.urlencode({"proposal": str(proposal), "repo": str(repo)})
        status, page = _get(srv.url + "repair?" + query)
    finally:
        srv.stop()
    assert status == 200
    words = _text(page)
    assert "catch the error instead of raising" in words
    assert "src/app.py" in words and "1 file(s)" in words
    assert "not run by this page" in words
    assert "Approve this change" in words
    # The page names the token's first eight characters, and the full token is never printed in the page.
    from blackbox.repair.proposal import load

    token = load(str(proposal), repo=str(repo)).token()
    assert token[:8] in page
    assert token not in page, "the page must show the prefix a person types, not the whole capability"
    assert _sha(repo / "src" / "app.py") == before, "reviewing a proposal wrote to the repository"


def _apply(srv, proposal: Path, repo: Path, *, typed: str, confirm: str = "yes",
           origin: str | None = None, csrf: str | None = None) -> tuple[int, str]:
    status, _, location = _post(srv.url + "repair/apply",
                                {"csrf": csrf if csrf is not None else srv.token, "proposal": str(proposal),
                                 "repo": str(repo), "typed": typed, "confirm": confirm}, origin=origin)
    if status != 303:
        return status, ""
    assert location.startswith(f"/{srv.token}/result/"), location
    return _get(f"http://127.0.0.1:{srv.port}{location}")


def test_an_unticked_box_applies_nothing(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    before = _sha(repo / "src" / "app.py")
    from blackbox.repair.proposal import load

    token = load(str(proposal), repo=str(repo)).token()
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _apply(srv, proposal, repo, typed=token[:8], confirm="no")
    finally:
        srv.stop()
    assert status == 200
    assert "Refused" in page and "confirmation box" in page
    assert _sha(repo / "src" / "app.py") == before


def test_a_wrong_or_missing_typed_token_applies_nothing(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    before = _sha(repo / "src" / "app.py")
    srv = app_mod.start(str(data_dir))
    try:
        for typed in ("", "00000000", "deadbeef"):
            status, page = _apply(srv, proposal, repo, typed=typed)
            assert status == 200 and "Refused" in page, typed
            assert _sha(repo / "src" / "app.py") == before, typed
    finally:
        srv.stop()


def test_the_right_token_applies_once_and_a_second_attempt_refuses(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    from blackbox.repair.proposal import load

    token = load(str(proposal), repo=str(repo)).token()
    srv = app_mod.start(str(data_dir))
    try:
        status, page = _apply(srv, proposal, repo, typed=token[:8])
        assert status == 200 and "Applied" in page, _text(page)[:600]
        assert (repo / "src" / "app.py").read_bytes() == b"def handler():\n    return 'caught'\n"
        assert ".blackbox-repair" in page, "the page must say where the backup went"
        # The same proposal, approved again against the changed tree, cannot match: the token is recomputed.
        status, page = _apply(srv, proposal, repo, typed=token[:8])
        assert "Refused" in page
        assert (repo / "src" / "app.py").read_bytes() == b"def handler():\n    return 'caught'\n"
    finally:
        srv.stop()


def test_a_post_from_another_origin_or_without_the_form_token_is_refused(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    before = _sha(repo / "src" / "app.py")
    from blackbox.repair.proposal import load

    token = load(str(proposal), repo=str(repo)).token()
    srv = app_mod.start(str(data_dir))
    try:
        status, body, _ = _post(srv.url + "repair/apply",
                                {"csrf": srv.token, "proposal": str(proposal), "repo": str(repo),
                                 "typed": token[:8], "confirm": "yes"}, origin="https://evil.example")
        assert status == 403 and "cross-origin" in body
        status, body, _ = _post(srv.url + "repair/apply",
                                {"csrf": "wrong", "proposal": str(proposal), "repo": str(repo),
                                 "typed": token[:8], "confirm": "yes"})
        assert status == 403 and "form token" in body
        # The right origin, the right form token: proved by the test above, not by this one.
        status, _, _ = _post(srv.url.replace(srv.token, "wrong-token") + "repair/apply",
                             {"csrf": srv.token, "proposal": str(proposal), "repo": str(repo),
                              "typed": token[:8], "confirm": "yes"})
        assert status == 404
    finally:
        srv.stop()
    assert _sha(repo / "src" / "app.py") == before


# ── the tests button ────────────────────────────────────────────────────────
def test_the_tests_button_runs_what_it_shows_and_refuses_without_the_tick(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    srv = app_mod.start(str(data_dir))
    command = sys.executable + " -c \"print('the command ran')\""
    try:
        status, _, location = _post(srv.url + "repair/tests",
                                    {"csrf": srv.token, "repo": str(repo), "command": command, "confirm": "no"})
        assert status == 303
        _, page = _get(f"http://127.0.0.1:{srv.port}{location}")
        assert "Refused" in page and "confirmation box" in page

        status, _, location = _post(srv.url + "repair/tests",
                                    {"csrf": srv.token, "repo": str(repo), "command": command, "confirm": "yes"})
        assert status == 303
        _, page = _get(f"http://127.0.0.1:{srv.port}{location}")
    finally:
        srv.stop()
    words = _text(page)
    assert "Verification:" in words and "passed" in words
    assert "the command ran" in words, "the output tail must be shown, not just an exit code"


def test_the_tests_button_refuses_an_empty_command_list(data_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    srv = app_mod.start(str(data_dir))
    try:
        status, _, location = _post(srv.url + "repair/tests",
                                    {"csrf": srv.token, "repo": str(repo), "command": "", "confirm": "yes"})
        assert status == 303
        _, page = _get(f"http://127.0.0.1:{srv.port}{location}")
    finally:
        srv.stop()
    assert "Refused" in page and "unverified" in page


# ── the gate is in this module, not only in the page ────────────────────────
def test_the_flows_refuse_without_a_human_even_when_called_directly(tmp_path: Path) -> None:
    from blackbox.ui import flows

    repo = _repo(tmp_path)
    proposal = _proposal(tmp_path, repo)
    with pytest.raises(flows.FlowRefused):
        flows.apply_reviewed(str(proposal), str(repo), typed="", confirmed=True)
    with pytest.raises(flows.FlowRefused):
        flows.apply_reviewed(str(proposal), str(repo), typed="00000000", confirmed=True)
    with pytest.raises(flows.FlowRefused):
        flows.apply_reviewed(str(proposal), str(repo), typed="whatever", confirmed=False)
    with pytest.raises(flows.FlowRefused):
        flows.run_tests(["echo hi"], cwd=str(repo), confirmed=False)
    assert (repo / "src" / "app.py").read_bytes() == b"def handler():\n    raise RuntimeError('boom')\n"


def test_a_proposal_that_escapes_the_repository_is_refused_by_the_review(tmp_path: Path) -> None:
    from blackbox.ui import flows

    repo = _repo(tmp_path)
    document = {"schema": SCHEMA, "title": "escape", "rationale": "", "tests": [], "risk": [],
                "changes": [{"path": "../outside.txt", "expected_sha256": None, "content": "x"}]}
    proposal = tmp_path / "escape.json"
    proposal.write_bytes(json.dumps(document).encode())
    view = flows.review_proposal(str(proposal), str(repo))
    assert view.ok is False and view.error
    assert not (tmp_path / "outside.txt").exists()
