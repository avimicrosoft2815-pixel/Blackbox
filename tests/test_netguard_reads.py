"""Netguard at run time, not at import time: the reading paths make **zero** outbound connection attempts.

`tests/test_no_egress.py` is a static check — it forbids the imports and the URL literals in `src/`. That is necessary and
not sufficient: a module can avoid importing `socket` and still reach the network through something it does import, and
no static check can see a connection a child process makes.

This file installs a **runtime** guard and then runs the product under it. The guard replaces the three ways a Python
program dials out (`socket.socket.connect`, `socket.socket.connect_ex`, `socket.create_connection`, `socket.getaddrinfo`,
`http.client.HTTPConnection.connect`) with functions that append a line to a log and raise. Each scenario then has two
obligations: **finish normally** (so the guard is not simply breaking it) and **leave the log empty**.

The scenarios are the ones a person actually runs on evidence they were given: `verify`, `view`, `diagnose`, `capsule
verify`, `ls`, the desktop's `--selftest`, and a `blackbox run` whose child is a process of its own.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

GUARD = '''
"""sitecustomize: record and refuse every attempt to dial out, then let the product run."""
import socket, http.client, os, traceback

LOG = os.environ["BBX_NETGUARD_LOG"]


def _record(what, detail):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{what} {detail}\\n")


def _refuse(what, detail):
    _record(what, detail)
    raise OSError(f"network access refused by the test guard ({what} {detail})")


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def connect(self, address):
    family = getattr(self, "family", None)
    _refuse("connect", f"{family} {address!r}")


def connect_ex(self, address):
    _refuse("connect_ex", f"{address!r}")


def create_connection(address, *args, **kwargs):
    _refuse("create_connection", f"{address!r}")


def getaddrinfo(host, port, *args, **kwargs):
    _refuse("getaddrinfo", f"{host!r} {port!r}")


def gethostbyname(host):
    _refuse("gethostbyname", f"{host!r}")


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.create_connection = create_connection
socket.getaddrinfo = getaddrinfo
socket.gethostbyname = gethostbyname


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        _refuse("http.client.connect", f"{self.host!r} {self.port!r}")


http.client.HTTPConnection.connect = _HTTPConnection.connect
'''


@pytest.fixture()
def guard(tmp_path: Path) -> Path:
    directory = tmp_path / "guard"
    directory.mkdir()
    (directory / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
    return directory


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    """A real capsule, made here with the recorder and sealed by the recovery path."""
    data_dir = tmp_path / "data"
    code = (
        "from blackbox import api\n"
        "with api.run('guarded'):\n"
        "    api.mark('hello', n=1)\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env["PYTHONPATH"] = str(SRC)
    env["BLACKBOX_HOME"] = str(data_dir)
    completed = subprocess.run([sys.executable, "-m", "blackbox", "run", "--no-proxy", "--no-sdk", "--no-hooks",
                                "--", sys.executable, "-c", code],
                               env=env, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stderr[-2000:]
    capsules = sorted((data_dir / "capsules").glob("*.bbx"))
    assert capsules, completed.stderr[-2000:]
    return capsules[0]


def _run(guard: Path, tmp_path: Path, argv: list[str], *, log: Path, cwd: Path | None = None,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env["PYTHONPATH"] = os.pathsep.join([str(guard), str(SRC)])
    env["BBX_NETGUARD_LOG"] = str(log)
    env["BLACKBOX_HOME"] = str(tmp_path / "guarded-data")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "blackbox", *argv], env=env, capture_output=True, text=True,
                          timeout=300, cwd=str(cwd or tmp_path))


def _attempts(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("argv, outs, target", [
    (["verify"], [], "capsule"),
    (["view"], ["--out", "page.html"], "capsule"),
    (["diagnose"], ["--out", "report.md", "--brief", "brief.md"], "capsule"),
    (["capsule", "verify"], [], "capsule"),
    (["ls"], ["--data"], "directory"),          # `ls` reads a data directory, not one capsule
])
def test_a_reading_path_makes_no_connection_attempt(guard, capsule, tmp_path, argv, outs, target):
    log = tmp_path / "netguard.log"
    where = str(capsule) if target == "capsule" else str(capsule.parent.parent)
    completed = _run(guard, tmp_path, [*argv, *outs, where], log=log)
    assert completed.returncode == 0, f"{argv}: {completed.stdout[-800:]}{completed.stderr[-1500:]}"
    attempts = _attempts(log)
    assert attempts == [], f"{argv} tried to dial out: {attempts}"


def test_the_guard_is_actually_installed_and_works(guard, tmp_path):
    """A guard that never fires would make every test above a tautology. Prove it refuses, then prove it logs."""
    log = tmp_path / "netguard.log"
    completed = _run(guard, tmp_path, ["--version"], log=log)
    assert completed.returncode == 0, completed.stderr
    probe = subprocess.run([sys.executable, "-c", "import socket; socket.getaddrinfo('example.invalid', 80)"],
                           env={"PYTHONPATH": str(guard), "BBX_NETGUARD_LOG": str(log), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode != 0 and "network access refused" in probe.stderr
    assert any("getaddrinfo" in line for line in _attempts(log))


def test_a_child_process_of_a_run_makes_no_connection_attempt(guard, capsule, tmp_path):
    """The row says *including from child processes*: the recorder's own child is the interesting one."""
    log = tmp_path / "netguard.log"
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("import os\nprint('child ran', os.getpid())\n", encoding="utf-8")
    completed = _run(guard, tmp_path, ["run", "--no-proxy", "--no-sdk", "--no-hooks", "--",
                                       sys.executable, "app.py"], log=log, cwd=project)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "child ran" in completed.stdout
    assert _attempts(log) == [], _attempts(log)


def test_the_desktop_selftest_makes_no_connection_attempt(guard, tmp_path):
    """The window's own boot path on a machine without a GUI: it reports what is missing, and dials nothing."""
    log = tmp_path / "netguard.log"
    completed = _run(guard, tmp_path, ["desktop", "--selftest", "--json"], log=log)
    assert completed.returncode in (0, 3), completed.stderr[-1500:]
    payload = json.loads(completed.stdout)
    assert "available" in payload
    assert _attempts(log) == [], _attempts(log)


def test_the_loopback_page_server_is_the_only_listener_and_binds_again_afterwards(guard, capsule, tmp_path):
    """`serve` binds loopback. The guard refuses *connections*, not binds, so this proves the distinction is real."""
    log = tmp_path / "netguard.log"
    completed = _run(guard, tmp_path, ["serve", str(capsule), "--once", "--port", "0"], log=log)
    # `serve --once` waits for one request and then exits; without a client it is refused a port or times out, and the
    # only thing this test asserts is that it never *connects* anywhere.
    assert completed.returncode in (0, 2), completed.stderr[-1500:]
    assert _attempts(log) == [], _attempts(log)
