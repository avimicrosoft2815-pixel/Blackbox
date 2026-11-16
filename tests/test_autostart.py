"""P3B-03's last half: the child's interpreter installs the seam, and the run recovers every vantage it produced.

Each test spawns a **real child process**, because the promise is about what CPython does at startup — a `.pth` that
is not executed, a `sitecustomize` that is, a `meta_path` finder that fires when the client library appears — and none
of that can be faked in-process. The child never imports this package in its own code: that is the whole point of the
auto-import, so a test that imported the seam by hand would prove nothing.

Two decisions are measured here rather than asserted from memory (D-044): a `.pth` on `PYTHONPATH` is not executed by
`site`, and `sitecustomize.py` in the same directory is. If a future CPython changes that, this file's first test says
so and the mechanism can be revisited instead of rotting.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.routes import Route, Routes
from blackbox.rec import autostart, launcher

posix = pytest.mark.skipif(os.name != "posix", reason="the auto-import tests spawn real children and use os.pathsep")
pytestmark = posix

BODY = b'{"ok":true}'


class _Origin(BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        type(self).seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = do_POST

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Origin.seen = []
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}


def _run_child(code: str, data_dir: Path, env: dict[str, str] | None = None,
               run_id: str = "run-autostart") -> tuple[object, autostart.Startup]:
    child_env = dict(env or _env())
    child_env["BLACKBOX_RUN_ID"] = run_id
    startup = autostart.arm(child_env, data_dir=str(data_dir))
    import subprocess

    proc = subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True, text=True, timeout=120)
    startup.close()
    return proc, startup


def test_a_pth_on_pythonpath_is_not_executed_and_sitecustomize_is(tmp_path):
    """The measurement D-044 rests on. A `.pth` outside a site directory is inert; `sitecustomize` is imported."""
    directory = tmp_path / "hookdir"
    directory.mkdir()
    (directory / "probe.pth").write_text('import sys; sys.stderr.write("PTH-RAN\\n")\n', encoding="utf-8")
    (directory / "sitecustomize.py").write_text('import sys; sys.stderr.write("SITE-RAN\\n")\n', encoding="utf-8")
    import subprocess

    both = subprocess.run([sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60,
                          env={**_env(), "PYTHONPATH": str(directory)})
    assert "SITE-RAN" in both.stderr, "sitecustomize must be imported at interpreter startup"
    assert "PTH-RAN" not in both.stderr, (
        "a `.pth` on PYTHONPATH is not executed — if this ever changes, the auto-import could use one again (D-044)")


def test_a_child_that_never_imports_a_client_library_says_so_instead_of_leaving_a_silent_vantage(tmp_path):
    data = tmp_path / "data"
    proc, startup = _run_child("print('quiet')", data)
    assert proc.returncode == 0, proc.stderr
    reports = autostart.read_reports(str(data))
    assert reports["errors"] == [], "the shim must not fail: it runs before the child's own code"
    (report,) = reports["startup"]
    assert report["armed"] is True
    assert report["seam"] is None and report["capture"] is False
    assert "never imported" in report["skipped"], report
    assert not os.path.exists(startup.directory), "the shim directory is removed when the child is gone"


def test_the_armed_child_pays_nothing_measurable_when_it_speaks_no_http(tmp_path):
    """The shim must not import this package at startup: that would cost every Python child ~25 ms for nothing."""
    code = ("import sys; print('BLACKBOX' if any(m.startswith('blackbox') for m in sys.modules) else 'CLEAN');"
            "print('sitecustomize' in sys.modules)")
    proc, _startup = _run_child(code, tmp_path / "data")
    out = proc.stdout.strip().splitlines()
    assert out[0] == "CLEAN", f"the shim imported the recorder at startup: {out}"
    assert out[1] == "True", "but it is on the path, which is how the seam gets installed when httpx arrives"


def test_a_child_that_imports_the_client_library_gets_the_seam_without_importing_us_in_its_own_code(tmp_path):
    data = tmp_path / "data"
    # The child's own code knows nothing about blackbox: it imports httpx2 and prints, exactly like a real app.
    proc, _startup = _run_child("import httpx2; httpx2.Client(); print('client-built')", data)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "client-built"
    (report,) = autostart.read_reports(str(data))["startup"]
    assert report["seam"] == "httpx-hooks", report
    assert report["capture"] is True
    assert report["attempts"] and report["attempts"][0]["status"] == "installed"


def test_a_sitecustomize_the_user_already_had_still_runs(tmp_path):
    """Ours is first on the path, so the one it shadows has to be imported by us or it would never run at all."""
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    marker = tmp_path / "theirs-ran.txt"
    (theirs / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    env = _env()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(theirs)])
    proc, _startup = _run_child("import httpx2; httpx2.Client()", tmp_path / "data", env=env)
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text(encoding="utf-8") == "ran", "the user's sitecustomize was shadowed and never ran"


def test_no_sdk_leaves_no_shim_directory_behind_and_the_run_says_why(tmp_path, origin, capfd):
    argv = [sys.executable, "-c", "print('child')"]
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin), env=_env(), sdk=False)
    assert result.exit_code == 0
    assert result.attach["sdk"]["armed"] is False
    assert result.attach["sdk"]["reason"] == "--no-sdk"
    assert result.attach["sdk"]["reports"] == []
    summary = Path(tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "sdk httpx" not in summary, f"nothing was armed, so no seam may be named: {summary}"


def _routes(origin_port: int) -> Routes:
    return Routes([Route(name="openai", base=f"http://127.0.0.1:{origin_port}", prefix="/openai"),
                   Route(name="anthropic", base=f"http://127.0.0.1:{origin_port}", prefix="/anthropic")],
                  default="openai")


CHILD = r"""
import json, os
import httpx2

base = os.environ["OPENAI_BASE_URL"]
response = httpx2.Client().post(base + "/chat/completions",
                                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
print("CHILD " + json.dumps({"status": response.status_code, "forced": os.environ.get("OPENAI_BASE_URL")}))
"""


def test_the_launcher_records_the_sdk_vantage_and_recovers_it_alongside_its_own(tmp_path, origin, capfd):
    """The whole ticket, end to end: a real child, a real request, and two tapes of one run at exit."""
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch([sys.executable, str(script)], data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=_env())
    printed = json.loads(next(line for line in capfd.readouterr().out.splitlines()
                              if line.startswith("CHILD "))[len("CHILD "):])
    assert printed["status"] == 200, printed
    assert result.proxy["stats"]["requests"] == 1, "the proxy saw the call"
    assert _Origin.seen == ["/v1/chat/completions"], "and so did the upstream, with the prefix stripped"
    sdk = result.attach["sdk"]
    assert sdk["armed"] is True and sdk["errors"] == []
    (report,) = sdk["reports"]
    assert report["seam"] == "httpx-hooks", report
    rows = result.attach["vantages"]
    roles = {row["role"]: row for row in rows}
    assert set(roles) == {"child", "launcher"}, f"one tape per vantage, both recovered: {rows}"
    for row in rows:
        assert row["run_id"] == result.run_id, "every tape of the run carries the same run id"
        assert "error" not in row, row
        assert row["capsule_path"] and row["capsule_root"], "each vantage was sealed into its own capsule"
    assert roles["child"]["vantage"] == "sdk", "the child's tape is the SDK vantage"
    assert result.recovered["run_id"] == result.run_id, "the launcher's own capsule is still the run's report"


def test_force_proxy_overrides_a_variable_and_prints_what_it_replaced(tmp_path, origin, capfd):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    theirs = f"http://127.0.0.1:{origin}"
    env = {**_env(), "OPENAI_BASE_URL": theirs}
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch([sys.executable, str(script)], data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=env, proxy_force=True)
    printed = json.loads(next(line for line in capfd.readouterr().out.splitlines()
                              if line.startswith("CHILD "))[len("CHILD "):])
    assert printed["forced"].startswith(result.proxy["url"]), "the child now points at the proxy"
    assert result.proxy["overridden"] == {"OPENAI_BASE_URL": theirs}, (
        "an override a reader can see is a decision; one they cannot is a recorder moving somebody's traffic")
    assert result.proxy["adopted"] == {}


def test_without_force_the_same_variable_is_adopted_and_the_child_keeps_it(tmp_path, origin, capfd):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    theirs = f"http://127.0.0.1:{origin}"
    env = {**_env(), "OPENAI_BASE_URL": theirs}
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch([sys.executable, str(script)], data_dir=str(tmp_path / "data"), out=handle,
                                 proxy=True, proxy_routes=_routes(origin), env=env)
    printed = json.loads(next(line for line in capfd.readouterr().out.splitlines()
                              if line.startswith("CHILD "))[len("CHILD "):])
    assert printed["forced"] == theirs
    assert result.proxy["adopted"] == {"OPENAI_BASE_URL": theirs}
    assert result.proxy["overridden"] == {}


def test_no_hooks_records_no_hook_vantage(tmp_path, origin):
    argv = [sys.executable, "-c", "import os; print(os.environ.get('BLACKBOX_INBOX'))"]
    with open(tmp_path / "log.txt", "w") as handle:
        result = launcher.launch(argv, data_dir=str(tmp_path / "data"), out=handle, proxy=True,
                                 proxy_routes=_routes(origin), env=_env(), hooks=False)
    assert result.exit_code == 0
    assert result.hooks == {}
    assert not (tmp_path / "data" / "inbox").exists() or not os.listdir(tmp_path / "data" / "inbox")


def test_python_commands_are_recognised_through_the_wrappers_people_actually_use():
    assert autostart.looks_like_python(["python3.12", "app.py"])
    assert autostart.looks_like_python(["/usr/bin/python3", "-m", "pytest"])
    assert autostart.looks_like_python(["uv", "run", "python", "app.py"])
    assert autostart.looks_like_python(["env", "FOO=1", "python", "app.py"])
    assert autostart.looks_like_python(["py", "-3", "app.py"])
    assert not autostart.looks_like_python(["node", "app.js"])
    assert not autostart.looks_like_python(["uv", "run", "node", "app.js"])
    assert not autostart.looks_like_python(["/bin/sh", "-c", "python app.py"])
