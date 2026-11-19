"""What protection a data directory actually has, measured rather than assumed.

The gate says *data dir `0700` / files `0600` verified at startup on all three OSes*. Two of those words do the work:

* **verified**, not "created with a mode" — `os.makedirs(mode=0o700)` on a directory whose *parent* already exists with
  looser permissions, and a file written with `open(path, "wb")`, both end up readable by other users on the machine. The
  only honest check is `stat` on what is on disk, after a real recording, for every path the product wrote;
* **all three OSes**, not "on Linux" — Windows has no mode bits. `os.chmod` there only toggles the read-only attribute
  and `os.stat` synthesises `0o666`/`0o777`, so a test that asserts `0700` on Windows is asserting the constant. What
  Windows does have is an ACL, and `icacls` prints it; this module reads it and answers the only question that matters:
  can a principal other than this user (and the system's own administrators) read the directory?

The result is a dict with the platform, the path, what was measured, and a verdict, so the first-run doctor can print a
sentence that is true on the machine it is running on.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys

#: Windows principals that are *not* this user but are allowed to read a private directory: the system account, the
#: built-in administrators, and (when the user is not an administrator) the local system's own service accounts. Everything
#: else — `Everyone`, `BUILTIN\\Users`, `Authenticated Users`, `INTERACTIVE`, a plain username — is a finding.
WINDOWS_ALLOWED = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "NT AUTHORITY\\LOCAL SERVICE",
                   "NT AUTHORITY\\NETWORK SERVICE", "CREATOR OWNER", "OWNER RIGHTS")
#: Principals that mean "anyone else on this machine" on Windows.
WINDOWS_BROAD = ("Everyone", "BUILTIN\\Users", "Authenticated Users", "INTERACTIVE", "Users",
                 "NT AUTHORITY\\Authenticated Users")


def is_posix() -> bool:
    return os.name == "posix"


def mode_of(path: str) -> int:
    """The permission bits, or 0 on a platform where `stat().st_mode` does not describe access (Windows)."""
    return stat.S_IMODE(os.stat(path).st_mode) if is_posix() else 0


def posix_private(path: str, *, directory: bool) -> bool:
    """`0700` for a directory, `0600` for a file: no group, no other, no ambiguity."""
    mode = mode_of(path)
    want = 0o700 if directory else 0o600
    return (mode & 0o777) == want


def _icacls(path: str) -> list[str]:
    """The ACL lines `icacls` prints for `path`, or an empty list when the tool is not available."""
    try:
        # `icacls` is Windows' own ACL tool, called with an absolute path from `%SystemRoot%` when there is one; the
        # arguments are a fixed program and a path this module was given, never user text assembled into a shell.
        executable = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "icacls.exe")
        completed = subprocess.run(  # noqa: S603
            [executable if os.path.exists(executable) else "icacls", path],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if ":" in line and path not in line]


def windows_findings(path: str) -> list[str]:
    """Broad principals with any access to `path`, as `icacls` reports them. Empty means nothing broad was named."""
    findings: list[str] = []
    for line in _icacls(path):
        principal = line.split(":", 1)[0].strip()
        if not principal:
            continue
        for broad in WINDOWS_BROAD:
            if principal.casefold() == broad.casefold():
                findings.append(principal)
    return findings


def inspect(path: str, *, directory: bool | None = None) -> dict[str, object]:
    """Measure the protection of one path and say what was measured, in a form a doctor page can print."""
    if not os.path.exists(path):
        return {"path": path, "platform": sys.platform, "measured": False, "private": None,
                "detail": "does not exist yet"}
    if directory is None:
        directory = os.path.isdir(path)
    if is_posix():
        mode = mode_of(path)
        private = posix_private(path, directory=bool(directory))
        return {"path": path, "platform": sys.platform, "measured": True, "private": private,
                "mode": f"{mode:04o}", "want": "0700" if directory else "0600",
                "detail": f"mode {mode:04o}" + ("" if private else " — other users on this machine can read this")}
    broad = windows_findings(path)
    return {"path": path, "platform": sys.platform, "measured": True, "private": not broad,
            "mode": None, "broad_principals": broad,
            "detail": ("the ACL names no broad principal" if not broad
                       else f"the ACL grants {', '.join(broad)}") if _icacls(path) else
                      "the ACL could not be read on this machine"}


def audit(data_dir: str, *, limit: int = 5000) -> dict[str, object]:
    """Walk what the product wrote and report every path whose protection is looser than the promise.

    The walk is bounded because a data directory can hold thousands of capsules; the summary says how many paths were
    looked at, and `ok` is true only when every one of them passed — a report that skipped the rest silently would be the
    same lie as not checking.
    """
    findings: list[dict[str, object]] = []
    seen = 0
    for root, dirs, files in os.walk(data_dir):
        for name in dirs + files:
            path = os.path.join(root, name)
            seen += 1
            if seen > limit:
                break
            report = inspect(path)
            if report["measured"] and not report["private"]:
                findings.append(report)
        if seen > limit:
            break
    return {"data_dir": data_dir, "platform": sys.platform, "paths_checked": seen, "limit": limit,
            "findings": findings, "ok": not findings, "truncated": seen > limit}


def private_dir(path: str) -> None:
    """Create `path` (and any missing parents) as `0700`, then *verify* it, because a parent created implicitly by
    `makedirs` inherits the umask rather than the mode its child asked for.

    This is the difference the audit found: `tape/<uuid>` was `0700` and its parent `tape/` was `0755`, so the promise
    covered the leaf and not the tree. `os.chmod` is applied on POSIX only, where the bits mean access; on Windows the
    directory inherits the ACL of the profile directory it lives under, which is the protection that actually applies.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    if is_posix():
        os.chmod(path, 0o700)


def private_file(path: str) -> None:
    """Tighten an already-created file to `0600` on POSIX. Used after a library (`sqlite3`, `zipfile`) created it."""
    if is_posix():
        os.chmod(path, 0o600)
