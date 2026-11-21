"""The beginner's journey, walked once from end to end through the window's own pages.

Every step below already had a test of its own (`tests/test_ui_flows.py`, `tests/test_ui_library.py`,
`tests/test_repair_workflow.py`). What none of them did is walk the **chain**, in the order a person meets it, over the
server the window actually serves — so a page could lose the link to the next step and every test would still pass. That
is what this file is: one incident, one session, eight pages, no CLI in the middle.

    onboarding   what this machine can do, and what is recorded — and what is not
    record       a real child process, recorded by the real launcher, sealing a real capsule
    library      the run is listed by a name a person can click
    run          the run's events, with the three things a person does next
    check        the integrity index recomputed from the bytes
    diagnose     the findings, and the repair brief as text with "the app does not send it anywhere"
    repair       the proposal's diff, its risks, and the approval token — and nothing written yet
    apply        the exact proposal, after the token, with a backup
    checks       the commands it suggested, run because a person ticked the box

The machine cannot show a window (no display, no GTK, no WebKit): `docs/desktop.md` and `tools/desktop_journey.sh` own
that question. This file owns the other one — that the thing behind the window is a journey and not eight pages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.routes import Route, Routes
from blackbox.rec import launcher
from blackbox.ui import app as app_mod

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the journey starts a real child process (POSIX launcher)")

BODY = b'{"ok":true}'


class _Origin(BaseHTTPRequestHandler):
    """A provider-shaped origin: it answers, and it records that it was reached."""

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = do_POST

    def log_message(self, *args: object) -> None:
        pass


CHILD = r"""
import os, sys, urllib.request
base = os.environ["OPENAI_BASE_URL"]
with urllib.request.urlopen(urllib.request.Request(base + "/chat/completions", data=b"{}", method="POST"),
                            timeout=10) as response:
    print("the model answered", response.status, flush=True)
raise SystemExit(3)
"""


def _text(html_text: str) -> str:
    """The page as words: assertions here are about what a person reads, not about markup."""
    body = html_text[html_text.index("<body>"):]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _get(url: str) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


def _post(url: str, fields: dict[str, str]) -> tuple[int, str, str]:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Location", "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@pytest.fixture()
def incident(tmp_path: Path):
    """A recorded, sealed, failing run — made the way a user's own run is made, not hand-built."""
    origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    try:
        script = tmp_path / "app.py"
        script.write_text(CHILD, encoding="utf-8")
        data_dir = tmp_path / "data"
        log = tmp_path / "log.txt"
        routes = Routes([Route(name="openai", base=f"http://127.0.0.1:{origin.server_address[1]}",
                               prefix="/openai")], default="openai")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
        with open(log, "w") as handle:
            result = launcher.launch([sys.executable, str(script)], data_dir=str(data_dir), out=handle,
                                     proxy=True, proxy_routes=routes, env=env)
        assert result.exit_code == 3, f"the child exits 3 on purpose; the launcher said {result.exit_code}"
        assert result.capsules, "no capsule was sealed, so there is no incident to walk"
        yield data_dir, result
    finally:
        origin.shutdown()
        origin.server_close()


def _capsule_name(data_dir: Path) -> str:
    """The incident's own capsule: the one whose diagnosis has something to say.

    A proxied run leaves two capsules — the app's and the proxy's vantage (§11.9) — and a beginner is looking for the run
    that failed, not for the vantage that watched it. Choosing by "which one has findings" is also the honest test: if
    neither has any, the journey has nothing to walk and the assertion below says so.
    """
    from blackbox.ui import flows

    names = sorted(path.name for path in (data_dir / "capsules").glob("*.bbx"))
    assert names, "the launcher sealed no capsule"
    for name in names:
        if flows.diagnose_capsule(str(data_dir / "capsules" / name)).findings:
            return name
    pytest.fail(f"no capsule of this run has a finding to diagnose: {names}")


def _proposal(tmp_path: Path) -> tuple[Path, Path]:
    from blackbox.ui import flows

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    target = repo / "src" / "app.py"
    target.write_text("def handler():\n    raise RuntimeError('boom')\n", encoding="utf-8")
    document = {
        "schema": "blackbox.repair-proposal/1",
        "title": "catch the error instead of raising",
        "rationale": "the recording shows the handler raising and the process exiting 3",
        "tests": [sys.executable + ' -c "print(\'ok\')"'],
        "risk": ["changes control flow"],
        "changes": [{"path": "src/app.py",
                     "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                     "content": "def handler():\n    return 'caught'\n"}],
    }
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps(document, indent=2), encoding="utf-8")
    flows.review_proposal(str(proposal), str(repo))       # the token is derived from the diff; nothing is written here
    return proposal, repo


def test_the_eight_pages_of_the_journey_are_a_chain_a_person_can_walk(tmp_path: Path, incident) -> None:
    data_dir, result = incident
    name = _capsule_name(data_dir)
    server = app_mod.start(str(data_dir))
    base = f"http://127.0.0.1:{server.port}/{server.token}"
    try:
        # 1. onboarding: what this machine can do, and the promise about what is and is not recorded
        status, page, _ = _get(f"{base}/onboarding")
        assert status == 200
        words = _text(page)
        assert "What this machine can do" in words
        assert "Recorded" in words and "Not recorded" in words, "the first page has to say what is captured"
        assert f"{server.token}/library" in page, "onboarding must offer the way to the recorded runs"

        # 2 & 3. the incident is listed, by a name a person can click
        status, page, _ = _get(f"{base}/library")
        assert status == 200
        assert name in page, "the run the launcher just sealed is not in the library"
        assert f"{server.token}/run/{name}" in page

        # 4. the run itself, with the three things a person does next
        status, page, _ = _get(f"{base}/run/{name}")
        assert status == 200
        for next_step in (f"{server.token}/verify/{name}", f"{server.token}/diagnose/{name}", f"{server.token}/repair"):
            assert next_step in page, f"the run page does not offer {next_step}"

        # 5. check: recomputed from the bytes, and it says so
        status, page, _ = _get(f"{base}/verify/{name}")
        assert status == 200
        words = _text(page)
        assert "verified" in words.lower(), words[:400]
        assert "recomputed here, from the bytes in the file" in words.lower(), words[:600]

        # 6. diagnose: findings, and the brief — with the sentence that decides whether a beginner dares to use it
        status, page, _ = _get(f"{base}/diagnose/{name}")
        assert status == 200
        words = _text(page)
        assert "The repair brief" in words
        assert "does not send it anywhere" in words, "the brief's page must say where the brief goes: nowhere, yet"
        assert f"{server.token}/export/{name}/brief" in page

        # 6b. the brief, saved by the person (a download, not a write on the server)
        status, brief, headers = _get(f"{base}/export/{name}/brief")
        assert status == 200
        assert "attachment" in headers.get("Content-Disposition", "").lower(), headers
        assert "repair" in brief.lower() or "brief" in brief.lower(), brief[:300]

        # 7. repair: the diff, the risks and the token — and nothing written
        proposal, repo = _proposal(tmp_path)
        before = (repo / "src" / "app.py").read_bytes()
        status, page, _ = _get(f"{base}/repair?proposal={urllib.parse.quote(str(proposal))}"
                               f"&repo={urllib.parse.quote(str(repo))}")
        assert status == 200
        words = _text(page)
        assert "Approve this change" in words
        assert "Why it might be a bad idea" in words, "the review has to name the risk, not only the change"
        assert "begin" in words.lower() and "approval token" in words.lower(), words[-500:]
        assert (repo / "src" / "app.py").read_bytes() == before, "loading the review page wrote to the user's repository"

        # 8. apply, after the token — and the suggested checks after their own tick
        hint = re.search(r"begins\s*</?[^>]*>?\s*([0-9a-f]{8})", page)
        assert hint, "the page must print the first eight characters of the token it expects"
        status, _, location = _post(f"{base}/repair/apply",
                                    {"csrf": server.token, "proposal": str(proposal), "repo": str(repo),
                                     "confirm": "yes", "typed": hint.group(1)})
        assert status == 303, (status, location)
        assert location.startswith(f"/{server.token}/result/"), location
        status, page, _ = _get(f"http://127.0.0.1:{server.port}{location}")
        assert status == 200
        assert "Applied" in _text(page)
        assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "def handler():\n    return 'caught'\n"
        backups = list(repo.glob("**/*backup*")) + list(repo.glob(".blackbox*")) + list(repo.glob("**/.blackbox*"))
        assert backups, "applying has to leave the files it replaced somewhere a person can find them"

        status, _, location = _post(f"{base}/repair/tests",
                                    {"csrf": server.token, "repo": str(repo), "confirm": "yes",
                                     "command": sys.executable + ' -c "print(\'ok\')"'})
        assert status == 303, status
        status, page, _ = _get(f"http://127.0.0.1:{server.port}{location}")
        assert status == 200
        words = _text(page)
        assert "Verification" in words and "passed" in words.lower(), words[:500]
    finally:
        server.stop()


def test_the_journey_cannot_be_walked_past_its_gates(tmp_path: Path, incident) -> None:
    """The chain is only a journey if the steps that write cannot be skipped. Same session, the shortcuts refused."""
    data_dir, _result = incident
    name = _capsule_name(data_dir)
    server = app_mod.start(str(data_dir))
    base = f"http://127.0.0.1:{server.port}/{server.token}"
    try:
        proposal, repo = _proposal(tmp_path)
        before = (repo / "src" / "app.py").read_bytes()
        # No token typed: refused, said so on a page of its own, and the repository is untouched.
        status, _, location = _post(f"{base}/repair/apply",
                                    {"csrf": server.token, "proposal": str(proposal), "repo": str(repo),
                                     "confirm": "yes"})
        assert status == 303, status
        _status, page, _ = _get(f"http://127.0.0.1:{server.port}{location}")
        assert "Refused" in _text(page), "a refusal has to be a page, not a silence"
        assert (repo / "src" / "app.py").read_bytes() == before, "an apply without the typed token wrote a file"
        # No tick: refused the same way, and no command ran.
        status, _, location = _post(f"{base}/repair/tests", {"csrf": server.token, "repo": str(repo),
                                                             "command": sys.executable + ' -c "print(1)"'})
        assert status == 303, status
        _status, page, _ = _get(f"http://127.0.0.1:{server.port}{location}")
        assert "Refused" in _text(page), "the tests button ran a command nobody ticked abox for"
        # A capsule name that is a path, not a name: not found, not opened.
        status, _page, _ = _get(f"{base}/run/{urllib.parse.quote('../../etc/passwd')}")
        assert status == 404, status
        # And the run page still works, which is the point of a gate: it refuses the shortcut, not the person.
        status, page, _ = _get(f"{base}/run/{name}")
        assert status == 200 and name in page
    finally:
        server.stop()


def test_the_app_binds_loopback_and_nothing_else(incident) -> None:
    """A window that bound a public interface would be a recording service; this is a page on your own machine."""
    data_dir, _result = incident
    server = app_mod.start(str(data_dir))
    try:
        bound = server._httpd.server_address[0]
        assert bound == "127.0.0.1", f"the app bound {bound}, which is not loopback"
        with socket.create_connection(("127.0.0.1", server.port), timeout=5):
            pass                                  # reachable from this machine, which is the whole point
        assert server.token not in (server.endpoint,), "the token is a path component, never a host"
    finally:
        server.stop()
