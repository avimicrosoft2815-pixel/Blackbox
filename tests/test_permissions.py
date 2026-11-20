"""`0700` for the data directory and `0600` for its files — measured after a real run, on the OS the test runs on.

The gate says *all three OSes*, and the tempting way to write this test is `assert mode == 0o700` and let it pass on Linux
CI. That would be a test of one platform wearing three platform names: on Windows, `os.stat` synthesises mode bits from
the read-only attribute, so `0700` there is a constant and the assertion is decoration.

So the file has two arms and they measure different things:

* **POSIX** — run a real recording, then `stat` **every** path the product wrote, files and directories, and require
  `0600`/`0700` exactly. Not "at least as tight as", which would let `0640` pass; exactly, because a group bit is a
  second reader by definition. This arm found a real defect: `meta`, `seal`, `index.sqlite`, `tape/` and `runs/` were
  created at `0644`/`0755` while the leaf directory was `0700`, so the promise covered the tree's leaves and not its
  branches. `platform/perms.private_dir`/`private_file` are the fix, and this is their regression test.
* **Windows** — read the ACL with `icacls` and fail if a principal that means "other users on this machine" (Everyone,
  BUILTIN\\Users, Authenticated Users, INTERACTIVE) has any access. The current user, SYSTEM and Administrators are
  expected; anything broader is the finding the gate is about. On a machine where `icacls` cannot be run the test skips
  with that sentence rather than claiming a pass.

The startup verification the row asks for is the same measurement used by the first-run doctor
(`ui/onboarding._data_dir_check` → `platform/perms.audit`), so a user sees the warning on a directory that was made by an
older build, or unpacked with the wrong permissions, rather than only in CI.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.platform import perms


def _real_run(data_dir: Path, *, build_index: bool = True) -> None:
    """One real recording through the front door, then `blackbox index`: every writer the product has, in one place."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLACKBOX")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["BLACKBOX_HOME"] = str(data_dir)
    completed = subprocess.run([sys.executable, "-m", "blackbox", "run", "--no-proxy", "--no-sdk", "--no-hooks",
                                "--", sys.executable, "-c", "print('permissions')"],
                               env=env, capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert any(data_dir.rglob("*.bbx")), "the run produced no capsule, so there is nothing to measure"
    if build_index:
        indexed = subprocess.run([sys.executable, "-m", "blackbox", "index"], env=env, capture_output=True, text=True,
                                 timeout=600)
        assert indexed.returncode == 0, indexed.stderr[-2000:]
        assert (data_dir / "index").is_dir(), "`blackbox index` wrote nothing, so the permissions test is narrower"


@pytest.mark.skipif(not perms.is_posix(), reason="mode bits are a POSIX concept; the Windows arm reads the ACL instead")
def test_every_path_a_run_writes_is_0600_or_0700(tmp_path):
    data_dir = tmp_path / "data"
    _real_run(data_dir)

    offenders: list[str] = []
    files = directories = 0
    for root, dirs, names in os.walk(data_dir):
        for name in dirs:
            directories += 1
            path = os.path.join(root, name)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != 0o700:
                offenders.append(f"{path}: directory {mode:04o}, want 0700")
        for name in names:
            files += 1
            path = os.path.join(root, name)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != 0o600:
                offenders.append(f"{path}: file {mode:04o}, want 0600")
    assert directories >= 4 and files >= 6, f"only {directories} directories and {files} files were written"
    assert not offenders, "paths a recording wrote are readable by other users:\n" + "\n".join(sorted(offenders))


@pytest.mark.skipif(not perms.is_posix(), reason="the Windows arm below is the one that measures this there")
def test_the_audit_agrees_with_the_walk_and_reports_a_loose_path(tmp_path):
    data_dir = tmp_path / "data"
    _real_run(data_dir)
    audit = perms.audit(str(data_dir))
    assert audit["ok"] is True, audit["findings"][:5]
    assert audit["paths_checked"] >= 10 and audit["truncated"] is False

    loose = data_dir / "loose.txt"
    loose.write_text("not a secret, but the mode is the point\n", encoding="utf-8")
    os.chmod(loose, 0o644)
    again = perms.audit(str(data_dir))
    assert again["ok"] is False
    assert [Path(finding["path"]).name for finding in again["findings"]] == ["loose.txt"]
    assert again["findings"][0]["mode"] == "0644"
    assert "read this" in str(again["findings"][0]["detail"])


def test_the_first_run_doctor_reports_the_permissions_it_measured(tmp_path):
    from blackbox.ui import onboarding

    data_dir = tmp_path / "data"
    _real_run(data_dir)
    report = onboarding.probe(str(data_dir))
    check = next(item for item in report["checks"] if item["id"] == "data_dir")
    if perms.is_posix():
        assert check["severity"] == "ok", check
        assert "0700/0600" in check["detail"], check
    else:
        assert "permissions win32" in check["detail"], check


def test_a_loose_directory_is_a_warning_with_a_command_that_fixes_it(tmp_path):
    from blackbox.ui import onboarding

    if not perms.is_posix():
        pytest.skip("chmod is the POSIX remedy; the Windows arm's remedy is icacls and is asserted in its own test")
    data_dir = tmp_path / "data"
    _real_run(data_dir)
    os.chmod(data_dir / "capsules", 0o755)  # noqa: S103 — making a directory loose on purpose; that is the fixture
    check = next(item for item in onboarding.probe(str(data_dir))["checks"] if item["id"] == "data_dir")
    assert check["severity"] == "warn", check
    assert "readable by other users" in check["title"]
    assert check["command"].startswith("chmod -R go-rwx ")


@pytest.mark.skipif(sys.platform != "win32", reason="icacls exists only on Windows; this arm is measured in Windows CI")
def test_on_windows_no_broad_principal_has_access(tmp_path):
    data_dir = tmp_path / "data"
    _real_run(data_dir)
    findings = perms.windows_findings(str(data_dir))
    assert findings == [], f"{', '.join(findings)} can read the data directory"
    report = perms.inspect(str(data_dir))
    assert report["measured"] is True and report["private"] is True, report
    assert report["mode"] is None, "Windows has no mode bits to report; claiming one would be the bug"
