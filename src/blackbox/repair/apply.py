"""Applying a proposal under an approval token, and the two things that make it survivable: re-checks and rollback.

The first version of this file trusted the state `load()` had seen. That is a time-of-check/time-of-use gap, and it is
the gap that matters here, because the thing on the other side of it is a file the user did not review:

1. **Re-check at write time.** Every target is re-validated immediately before its own write — still inside the
   repository, no symlink in any component, still the hash that was approved. A file swapped for a symlink between the
   preview and the apply is refused at the moment it matters, not at the moment it was read.
2. **Do not follow a link when reading or writing.** Originals are read with `O_NOFOLLOW`, temporaries are created with
   `O_CREAT|O_EXCL|O_NOFOLLOW`, and `os.replace` renames *over* a link rather than through it. On Windows `O_NOFOLLOW`
   does not exist; the `islink` checks still run, and `docs/DECISIONS.md` D-050 records that check-then-write window as
   the limit of the guarantee on that platform rather than pretending it is closed. Every descriptor here goes through
   `platform().open_fd`, which sets `O_BINARY`: on Windows the CRT defaults to text mode and would translate `0x0A` on
   the way in *and* out, corrupting both the bytes being backed up and the bytes being restored.
3. **Back up inside the repository, verified.** The backup directory is `<repo>/.blackbox-repair/backup-…`, created
   `0700`, refused if it is (or is under) a symlink, and every copy is re-hashed after writing: a backup that silently
   failed is worse than no backup, because the rollback would then restore the wrong bytes.
4. **A manifest before the first mutation.** `apply-manifest.json` records the planned paths, their approved hashes and
   the backup directory *before* anything changes, so a process killed mid-apply leaves an auditable record even though
   it left no chance to roll back.
5. **Rollback that does not depend on the failing mechanism.** Restores use their own plain write-and-retry path, run in
   reverse, attempt *every* file rather than stopping at the first restore failure, and name any file they could not put
   back. A rollback that reports success while a file is still wrong is the worst outcome of the four.

`verify()` runs commands, and that is the one place this package executes anything: only strings a human passes on the
command line, in an explicit directory, with the exit code and output tail reported. Nothing from a proposal is run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from blackbox.platform import current as platform
from blackbox.repair.proposal import NOFOLLOW, Proposal, ProposalError, _check_path, _read, read_nofollow, sha256_file

#: `NOFOLLOW` comes from `proposal` so the read path and the write path cannot disagree about it; on Windows it is 0,
#: the `islink` checks are what refuse a link, and the remaining exposure is a swap between check and open (D-050).
BACKUP_DIR = ".blackbox-repair"


@dataclass
class Applied:
    written: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    backup_dir: str | None = None
    #: `True` when the backup directory's 0700 was enforced and checked; `False` on a platform without POSIX modes,
    #: where the caller has to say so rather than let a reader assume the backups are private.
    private_by_mode: bool = True
    restored: list[str] = field(default_factory=list)
    restore_failures: list[str] = field(default_factory=list)
    manifest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"written": self.written, "created": self.created, "backup_dir": self.backup_dir,
                "private_by_mode": self.private_by_mode, "restored": self.restored,
                "restore_failures": self.restore_failures, "manifest": self.manifest}


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Whether `chmod` mode bits describe access on this platform. On Windows they do not: `os.chmod` can only toggle the
#: read-only attribute, and `os.stat` synthesises `0o666`/`0o777` whatever the ACL actually says. A "0700 backup
#: directory" is therefore not expressible there, and `apply()` reports which guarantee the caller got instead of
#: asserting one that does not exist.
MODES_ARE_REAL = os.name == "posix"


#: Whether `chmod` mode bits describe access on this platform. On Windows they do not: `os.chmod` can only toggle the
#: read-only attribute, and `os.stat` synthesises `0o666`/`0o777` whatever the ACL actually says. A "0700 backup
#: directory" is therefore not expressible there, and `apply()` reports which guarantee the caller got instead of
#: asserting one that does not exist.
MODES_ARE_REAL = os.name == "posix"


#: Whether `chmod` mode bits describe access on this platform. On Windows they do not: `os.chmod` can only toggle the
#: read-only attribute, and `os.stat` synthesises `0o666`/`0o777` whatever the ACL actually says. A "0700 backup
#: directory" is therefore not expressible there, and `apply()` reports which guarantee the caller got instead of
#: asserting one that does not exist.
MODES_ARE_REAL = os.name == "posix"


#: Whether `chmod` mode bits describe access on this platform. On Windows they do not: `os.chmod` can only toggle the
#: read-only attribute, and `os.stat` synthesises `0o666`/`0o777` whatever the ACL actually says. A "0700 backup
#: directory" is therefore not expressible there, and `apply()` reports which guarantee the caller got instead of
#: asserting one that does not exist.
MODES_ARE_REAL = os.name == "posix"


def _assert_safe_target(path: str, *, repo_real: str) -> None:
    """The path checks that have to hold at *write* time, not only at read time."""
    real = os.path.realpath(path)
    if real != repo_real and not real.startswith(repo_real + os.sep):
        raise ProposalError(f"{path} resolves outside {repo_real} at write time")
    if os.path.islink(path):
        raise ProposalError(f"{path} became a symlink after it was approved; refusing to write through it")
    # Every directory between the root and the file, not only the immediate parent: a swap one level up redirects the
    # write just as effectively. `relpath` is taken against the resolved root, so the root's own ancestors are excluded
    # for the same reason `_check_path` excludes them — they are the platform's temp directory, not the proposal's.
    parent = os.path.dirname(path) or "."
    rel_parent = os.path.relpath(parent, repo_real)         # lexical on purpose: `realpath` would erase the link
    if rel_parent == ".." or rel_parent.startswith(".." + os.sep):
        raise ProposalError(f"{path} resolves outside {repo_real} at write time")
    if rel_parent != ".":
        walked = repo_real
        for part in rel_parent.split(os.sep):
            walked = os.path.join(walked, part)
            if os.path.islink(walked):
                raise ProposalError(f"{walked} became a symlink after it was approved; refusing to write through it")
    if not os.path.isdir(os.path.realpath(parent)):
        raise ProposalError(f"{parent} is no longer a directory")


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    """Write beside the target and rename over it: no reader ever sees a half-written file, and no link is followed."""
    tmp = f"{path}.bbx-repair-{os.getpid()}.tmp"
    fd = platform().open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, data)
            # Through the platform layer, like every other durability call in this package: Windows needs `_commit`,
            # macOS may want `F_FULLFSYNC`, and dispatch keeps that decision in one place (P2-03/P2-04).
            platform().fdatasync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)                       # renames over a link, never through it
        platform().fsync_dir(os.path.dirname(path) or ".")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _restore_plain(path: str, data: bytes, mode: int, *, attempts: int = 3) -> None:
    """Put original bytes back without relying on the mechanism that just failed.

    Deliberately not `_atomic_write`: if the atomic path is what broke (a full disk, a hostile directory), retrying it is
    how a rollback fails for the same reason as the apply. A truncating write to a fresh descriptor is the simplest thing
    that can work, and the caller records failure rather than raising into the middle of a multi-file restore.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            if os.path.islink(path):
                os.unlink(path)
            fd = platform().open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW, mode)
            try:
                os.write(fd, data)
                platform().fdatasync(fd)
            finally:
                os.close(fd)
            os.chmod(path, mode)
            return
        except OSError as exc:                       # pragma: no cover - exercised by the failure-injection test
            last = exc
            time.sleep(0.01 * (attempt + 1))
    raise OSError(f"could not restore {path}: {last}")


def _changed_since_preview(proposal: Proposal) -> list[str]:
    """Files that no longer match the hash the proposal was *reviewed* against."""
    stale = []
    for change in proposal.changes:
        now = sha256_file(change.absolute)
        if change.is_new():
            if now is not None:
                stale.append(f"{change.path} (it did not exist when this was reviewed)")
        elif now != change.expected_sha256:
            stale.append(f"{change.path} (reviewed {str(change.expected_sha256)[:8]}, now {str(now)[:8]})")
    return stale


def _prepare(proposal: Proposal) -> list[tuple[str, bytes | None, int, str]]:
    """Re-validate every target **now**, and read the originals without following links.

    Returns `(absolute, original_or_None, mode, expected_hash_of_content)` per change. Raises `ProposalError` before any
    write if a path has escaped, become a link, or no longer matches what was approved — the three ways the tree can move
    under a reviewer.
    """
    repo_real = proposal.repo
    prepared: list[tuple[str, bytes | None, int, str]] = []
    for change in proposal.changes:
        try:
            absolute = _check_path(repo_real, change.path)
        except ProposalError as exc:
            raise ProposalError(f"{change.path}: {exc}") from exc
        _assert_safe_target(absolute, repo_real=repo_real)
        if os.path.exists(absolute):
            original = read_nofollow(absolute)
            current = _sha_bytes(original)
            if change.is_new():
                raise ProposalError(f"{change.path} exists now but the proposal said it was new")
            if current != change.expected_sha256:
                raise ProposalError(
                    f"{change.path} changed after it was approved (reviewed {str(change.expected_sha256)[:8]}, "
                    f"now {current[:8]}); nothing was written")
            mode = os.stat(absolute).st_mode & 0o777
        else:
            if not change.is_new():
                raise ProposalError(f"{change.path} no longer exists; the proposal expected to replace it")
            original, mode = None, 0o644
        prepared.append((absolute, original, mode, _sha_bytes(change.content.encode("utf-8"))))
    return prepared


def _private_dir(path: str, *, what: str) -> bool:
    """Make `path` private if mode bits mean anything here, and say which of those two things happened.

    Returns `True` when 0700 is enforced and verified, `False` when the platform has no POSIX modes to enforce. A
    filesystem that *has* them and ignores `chmod` is neither of those: it is a place this feature cannot keep its
    promise, so it refuses rather than proceeding quietly.
    """
    if not MODES_ARE_REAL:
        return False
    if os.stat(path).st_mode & 0o077:               # backups are the user's source code in plain text
        os.chmod(path, 0o700)
        if os.stat(path).st_mode & 0o077:
            raise ProposalError(
                f"{what} {path} cannot be made private (mode {oct(os.stat(path).st_mode & 0o777)} after chmod); "
                f"refusing to write backups a group or other could read")
    return True


def _backup_root(repo_real: str) -> tuple[str, bool]:
    """`<repo>/.blackbox-repair`, as private as this platform allows, and refused if anything about it is a link.

    Returns `(root, private_by_mode)`. `private_by_mode` is `False` on Windows, where a directory's mode bits do not
    describe access and the backup is only as private as the repository directory's ACLs — a fact the caller passes on
    rather than a property this function pretends to have.
    """
    root = os.path.join(repo_real, BACKUP_DIR)
    if os.path.islink(root):
        raise ProposalError(f"{root} is a symlink; refusing to write backups through it")
    if not os.path.isdir(root):
        os.makedirs(root, mode=0o700, exist_ok=True)
    private = _private_dir(root, what="the backup directory")
    if os.path.islink(root):                        # re-check after creating: `makedirs` follows an existing link
        raise ProposalError(f"{root} is a symlink; refusing to write backups through it")
    return root, private


def apply(proposal: Proposal, *, approval: str) -> Applied:
    """Apply only if `approval` matches the proposal as it is *now*. Never runs anything from the proposal."""
    if approval != proposal.token():
        stale = _changed_since_preview(proposal)
        detail = ("the approval token does not match this proposal and this repository state"
                  + (f"; changed since preview: {', '.join(stale)}" if stale else ""))
        raise ProposalError(detail + " — re-run `repair preview` and approve the token it prints")
    prepared = _prepare(proposal)
    repo_real = proposal.repo
    backup_root, private = _backup_root(repo_real)
    backup_dir = os.path.join(backup_root, f"backup-{time.strftime('%Y%m%dT%H%M%S')}-{proposal.token()[:8]}")
    if os.path.exists(backup_dir):
        raise ProposalError(f"{backup_dir} already exists; refusing to reuse a backup directory")
    os.makedirs(backup_dir, mode=0o700)
    _private_dir(backup_dir, what="this backup")

    result = Applied(backup_dir=backup_dir, private_by_mode=private)
    manifest_path = os.path.join(backup_dir, "apply-manifest.json")
    result.manifest = manifest_path
    planned = [{"path": change.path, "approved_sha256": change.expected_sha256,
                "new_sha256": prepared[i][3], "existed": prepared[i][1] is not None}
               for i, change in enumerate(proposal.changes)]
    _atomic_write(manifest_path, json.dumps(
        {"schema": "blackbox.repair-apply/1", "token": proposal.token(), "repo": repo_real,
         "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "state": "planned",
         "backup": {"dir": backup_dir, "private_by_mode": private,
                    "note": "0700, verified" if private else
                            "this platform has no POSIX modes: the directory is as private as the repository's ACLs"},
         "changes": planned}, indent=2).encode(), 0o600)

    applied_paths: list[str] = []
    try:
        for index, (change, (absolute, original, mode, new_sha)) in enumerate(zip(proposal.changes, prepared,
                                                                                strict=True)):
            if original is not None:
                copy = os.path.join(backup_dir, f"{index:02d}-{os.path.basename(change.path)}")
                _atomic_write(copy, original, 0o600)
                if read_nofollow(copy) != original:
                    raise ProposalError(f"the backup of {change.path} did not verify; nothing was written")
            # The last check before the write: the tree can have moved while the backups were being taken.
            _assert_safe_target(absolute, repo_real=repo_real)
            if sha256_file(absolute) != change.expected_sha256:
                raise ProposalError(f"{change.path} changed while the apply was running; rolling back")
            _atomic_write(change.absolute if os.path.exists(change.absolute) else absolute,
                          change.content.encode("utf-8"), mode)
            if _sha_bytes(read_nofollow(absolute)) != new_sha:
                raise ProposalError(f"{change.path} did not read back as written; rolling back")
            applied_paths.append(absolute)
            (result.created if change.is_new() else result.written).append(change.path)
    except BaseException as exc:
        _rollback(applied_paths, prepared, proposal, result)
        _mark_manifest(manifest_path, "rolled_back", result)
        restored_note = (f"restored {len(result.restored)} file(s)"
                         if not result.restore_failures else
                         f"COULD NOT restore: {', '.join(result.restore_failures)} — manual intervention required")
        raise ProposalError(f"apply failed ({type(exc).__name__}: {exc}); {restored_note}; backup: {backup_dir}") from exc
    _mark_manifest(manifest_path, "applied", result)
    return result


def _rollback(applied: list[str], prepared: list[tuple[str, bytes | None, int, str]], proposal: Proposal,
              result: Applied) -> None:
    """Restore in reverse, attempt every file, and record what could not be restored.

    Stopping at the first restore failure would leave the rest of the tree half-applied with no report of it, which is
    the failure mode this function exists to avoid: a rollback that raises is a rollback nobody can act on.
    """
    by_path = {absolute: (original, mode) for absolute, original, mode, _ in prepared}
    for absolute in reversed(applied):
        original, mode = by_path[absolute]
        try:
            if original is None:
                if os.path.exists(absolute):
                    os.unlink(absolute)
            else:
                _restore_plain(absolute, original, mode)
            result.restored.append(os.path.relpath(absolute, proposal.repo))
        except OSError:
            result.restore_failures.append(os.path.relpath(absolute, proposal.repo))


def _mark_manifest(manifest_path: str, state: str, result: Applied) -> None:
    try:
        document = json.loads(_read(manifest_path).decode("utf-8"))
    except Exception:                                            # pragma: no cover - the manifest is ours
        document = {"schema": "blackbox.repair-apply/1"}
    document.update({"state": state, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "restored": result.restored, "restore_failures": result.restore_failures})
    # A failure to write the report must not mask the result it is reporting; the caller already has the outcome.
    with contextlib.suppress(OSError):                           # pragma: no cover - a full disk during the report
        _atomic_write(manifest_path, json.dumps(document, indent=2).encode(), 0o600)


def verify(commands: list[str], *, cwd: str, timeout_s: float = 900.0) -> dict[str, Any]:
    """Run the commands a human named, and report what happened. Nothing is inferred.

    **These commands are executed.** Each one is a string the caller supplied, run through the platform's shell in `cwd`,
    which means it can do anything that user can do. That is the point — a verification step that does not run anything
    verifies nothing — and it is why the command never comes from a proposal, a capsule or a brief, and why the CLI prints
    each command before running it. With no commands, the change is reported as unverified rather than as passing.
    """
    ran: list[dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        try:
            completed = subprocess_run(command, cwd=cwd, timeout_s=timeout_s)
            code, output = completed[0], completed[1]
        except TimeoutError:
            code, output = 124, f"timed out after {timeout_s:g}s"
        except OSError as exc:
            code, output = 127, f"could not run: {exc}"
        ran.append({"command": command, "exit_code": code, "seconds": round(time.monotonic() - started, 3),
                    "shell": True, "cwd": cwd,
                    "tail": output.strip().splitlines()[-20:]})
    failed = [row for row in ran if row["exit_code"] != 0]
    return {"ran": ran, "count": len(ran), "failed": len(failed), "verified": bool(ran) and not failed,
            "executes": True,
            "note": ("no command was run, so this change is unverified" if not ran
                     else "these commands ran in this process, just now, through the platform shell in the directory "
                          "named above; the proposal's suggested tests were not executed")}


def subprocess_run(command: str, *, cwd: str, timeout_s: float) -> tuple[int, str]:
    """One shell command. Isolated here so the fact that this package executes anything is one function, not a habit."""
    import subprocess

    try:
        completed = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,  # noqa: S602
                                   timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(str(exc)) from exc
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
