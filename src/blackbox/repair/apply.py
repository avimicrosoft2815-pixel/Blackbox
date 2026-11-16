"""Applying a proposal, and verifying what was actually run afterwards.

Two doors, both narrow:

* `apply(proposal, token)` — refuses unless the token matches one recomputed from the **current** repository state, so a
  proposal that was edited, or a file that moved underneath it, aborts before a single byte is written. Writes are
  atomic per file (temp + `os.replace`), the original mode is preserved, originals are copied to a backup directory first,
  and a failure at any point restores every file already written and removes the ones it created.
* `verify(commands, cwd)` — runs the commands a **human passed on the command line**, records the exit code and the tail
  of the output, and reports which ones actually ran. With no commands it says the change is unverified and exits
  non-zero: "no tests were run" is a result, and pretending otherwise is the failure this product exists to catch.

The proposal's own `tests` list is never executed. It is displayed by `preview` as a suggestion; a suggestion is not a
command, and evidence from a traced process is not a source of authority.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from blackbox.platform import current as platform
from blackbox.repair.proposal import Proposal, ProposalError, _read, sha256_file


@dataclass
class Applied:
    written: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    backup_dir: str | None = None
    restored: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"written": self.written, "created": self.created, "backup_dir": self.backup_dir,
                "restored": self.restored}


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    tmp = f"{path}.bbx-repair-tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            # Through the platform layer, like every other durability call in this package: Windows needs `_commit`,
            # macOS may want `F_FULLFSYNC`, and dispatch is what keeps that decision in one place (P2-03/P2-04).
            platform().fdatasync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _changed_since_preview(proposal: Proposal) -> list[str]:
    """Files that no longer match the hash the proposal was *reviewed* against.

    `current_sha256` is what the loader saw when it read the proposal — which, on an `apply`, is already the changed
    state. The reviewed state is `expected_sha256`, so that is what the message has to compare to: otherwise the run is
    refused (the token catches it) but the message names no file, which is the least useful way to be safe.
    """
    stale = []
    for change in proposal.changes:
        now = sha256_file(change.absolute)
        if change.is_new():
            if now is not None:
                stale.append(f"{change.path} (it did not exist when this was reviewed)")
        elif now != change.expected_sha256:
            stale.append(f"{change.path} (reviewed {str(change.expected_sha256)[:8]}, now {str(now)[:8]})")
    return stale


def apply(proposal: Proposal, *, approval: str) -> Applied:
    """Apply only if `approval` matches the proposal as it is *now*. Never runs anything from the proposal."""
    expected_token = proposal.token()
    if approval != expected_token:
        stale = _changed_since_preview(proposal)
        detail = ("the approval token does not match this proposal and this repository state"
                  + (f"; changed since preview: {', '.join(stale)}" if stale else ""))
        raise ProposalError(detail + " — re-run `repair preview` and approve the token it prints")
    backup_dir = os.path.join(os.path.dirname(proposal.path), ".blackbox-repair",
                              f"backup-{time.strftime('%Y%m%dT%H%M%S')}-{expected_token[:8]}")
    os.makedirs(backup_dir, exist_ok=True)
    result = Applied(backup_dir=backup_dir)
    originals: list[tuple[str, bytes | None, int]] = []
    try:
        for index, change in enumerate(proposal.changes):
            original = None if change.is_new() else _read(change.absolute)
            originals.append((change.absolute, original, change.mode))
            if original is not None:
                shutil.copy2(change.absolute, os.path.join(backup_dir, f"{index:02d}-{os.path.basename(change.path)}"))
            _atomic_write(change.absolute, change.content.encode("utf-8"), change.mode)
            (result.created if change.is_new() else result.written).append(change.path)
    except BaseException as exc:                       # any failure puts the tree back the way it was
        for path, original, mode in reversed(originals):
            try:
                if original is None:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    _atomic_write(path, original, mode)
                result.restored.append(path)
            except OSError:                            # pragma: no cover - a restore that fails is reported, not hidden
                result.restored.append(f"FAILED to restore {path}")
        raise ProposalError(f"apply failed ({type(exc).__name__}: {exc}); "
                            f"restored {len(result.restored)} file(s) from {backup_dir}") from exc
    return result


def verify(commands: list[str], *, cwd: str, timeout_s: float = 900.0) -> dict[str, Any]:
    """Run the commands a human named, and report what happened. Nothing is inferred."""
    ran: list[dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        try:
            completed = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,  # noqa: S602
                                       timeout=timeout_s)
            code, output = completed.returncode, (completed.stdout or "") + (completed.stderr or "")
        except subprocess.TimeoutExpired:
            code, output = 124, f"timed out after {timeout_s:g}s"
        except OSError as exc:
            code, output = 127, f"could not run: {exc}"
        ran.append({"command": command, "exit_code": code, "seconds": round(time.monotonic() - started, 3),
                    "tail": output.strip().splitlines()[-20:]})
    failed = [row for row in ran if row["exit_code"] != 0]
    return {"ran": ran, "count": len(ran), "failed": len(failed), "verified": bool(ran) and not failed,
            "note": ("no command was run, so this change is unverified" if not ran
                     else "these are the commands that ran, in this process, just now; the proposal's suggested tests "
                          "were not executed")}
