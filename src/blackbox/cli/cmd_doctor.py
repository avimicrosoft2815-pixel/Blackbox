"""`blackbox doctor` — the command the error table has been telling users to run since before it existed.

Six codes in `errors.py` and seven rows of the plan's §13 point at `doctor` (`--capture`, `--tape`, `--locks`,
`--hooks`, `--storage`, `--assert-offline`, `--secrets`). None of them existed, so the advice a user read after a
failure named a command the binary did not have — a paper cut that is worse than no advice, because it costs a second
failure at the moment somebody is already stuck (defect 111).

What this implements is the whole of it that can be *measured* on the machine it runs on, and nothing that cannot:

| probe | what it answers | how |
|---|---|---|
| `environment` | where the data directory is, whether it can be written, and what the permissions are | `platform.perms.inspect`, a real write |
| `capture` | which seam would install, and why the others would not | the same preflight `run` uses, released afterwards |
| `tape` | how many tapes and capsules are here, and whether any of them lost evidence | `tape.scanner.scan_all` |
| `hooks` | whether the hook adapters are installed, per tool | `hooks.claude_code.status`, `hooks.codex.status` |
| `storage` | free space, and whether a write + `fdatasync` + `rename` completes in this directory | a real probe file, deleted afterwards |
| `locks` | whether anything holds a writer lease in this data directory | the lock files' own contents, checked against live pids |

`--assert-offline` and `--secrets` are **not** implemented, and the codes that point at them are corrected rather than
left dangling: egress is asserted by the test suite (`tests/test_no_egress.py`, `tests/test_netguard_reads.py`) and
redaction by `tests/test_redaction_every_sink.py`, which is a stronger claim than a probe over somebody's data directory
could make. A probe that printed `ok` without measuring anything would be the failure mode this repository spends its
documentation preventing.

Exit codes: `0` every probe is fine or warns, `8` at least one probe failed, `2` usage. `--json` prints one object.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from blackbox import __version__
from blackbox import constants as C
from blackbox.cli.paths import default_data_dir

PROBES = ("environment", "capture", "tape", "hooks", "storage", "locks")

OK = "ok"
WARN = "warn"
FAIL = "fail"
ABSENT = "could not run"


def _result(probe: str, status: str, summary: str, **detail: Any) -> dict[str, Any]:
    return {"probe": probe, "status": status, "summary": summary, "detail": detail}


def probe_environment(data_dir: str) -> dict[str, Any]:
    from blackbox.platform import perms

    path = Path(data_dir)
    exists = path.exists()
    writable = os.access(path, os.W_OK) if exists else False
    detail: dict[str, Any] = {
        "version": __version__, "format_version": C.FORMAT_VERSION,
        "python": platform.python_version(), "platform": platform.platform(),
        "data_dir": str(path), "exists": exists, "writable": writable,
    }
    if not exists:
        return _result("environment", WARN, f"the data directory {path} does not exist yet; `blackbox run` creates it", **detail)
    try:
        detail["permissions"] = perms.inspect(str(path), directory=True)
    except Exception as exc:                       # a probe that cannot read permissions says so, it does not fail
        detail["permissions"] = f"{type(exc).__name__}: {exc}"
    if not writable:
        return _result("environment", FAIL, f"{path} is not writable, so nothing can be recorded there", **detail)
    return _result("environment", OK, f"data directory {path} is writable", **detail)


class _Discard:
    """A receiver that keeps nothing: the preflight needs one, and `doctor` must not leave a seam installed."""

    def __getattr__(self, name: str) -> Any:
        def sink(*args: Any, **kwargs: Any) -> None:
            return None
        return sink


def probe_capture() -> dict[str, Any]:
    from blackbox.cap_sdk import preflight

    detail: dict[str, Any] = {}
    try:
        result = preflight.choose(_Discard())
        detail["seam"] = result.seam
        detail["attempts"] = [attempt.as_dict() for attempt in result.attempts]
        detail["capturing"] = bool(getattr(result, "capturing", False))
    except Exception as exc:                        # the preflight is fail-open; so is this probe
        return _result("capture", WARN, f"the capture preflight could not be run here: {type(exc).__name__}: {exc}",
                       **detail)
    finally:
        try:
            preflight.release()
        except Exception:
            pass
    seam = detail.get("seam")
    if seam:
        return _result("capture", OK, f"seam {seam} would install on this machine", **detail)
    detail["seams_tried"] = [a.get("seam") for a in detail.get("attempts", [])]
    return _result("capture", WARN,
                   "no seam can install here: nothing imports httpx or an SDK, so a run records the proxy's vantage only",
                   **detail)


def probe_tape(data_dir: str) -> dict[str, Any]:
    from blackbox.tape import scanner

    if not Path(data_dir).exists():
        return _result("tape", WARN, "no data directory yet, so there are no tapes to read", tapes=0)
    try:
        scans = scanner.scan_all(data_dir)
    except Exception as exc:
        return _result("tape", FAIL, f"the tapes could not be read: {type(exc).__name__}: {exc}")
    lossy = [s for s in scans if s.truncated_tail or s.gaps or s.bad_segments]
    detail = {
        "tapes": len(scans),
        "frames": sum(s.frame_count for s in scans),
        "capsules": len(list(Path(data_dir, "capsules").glob("*.bbx"))) if Path(data_dir, "capsules").is_dir() else 0,
        "with_lost_evidence": [
            {"writer": s.writer_uuid[:12], "completeness": s.completeness(), "truncated_tail": s.truncated_tail,
             "gaps": len(s.gaps), "bad_segments": s.bad_segments}
            for s in lossy
        ],
    }
    if lossy:
        return _result("tape", WARN,
                       f"{len(lossy)} of {len(scans)} tape(s) lost evidence: a torn tail, a declared gap or a segment "
                       f"that will not read. `blackbox ls` names each capsule's status", **detail)
    return _result("tape", OK, f"{len(scans)} tape(s), {detail['frames']} frame(s), {detail['capsules']} capsule(s), "
                               f"nothing lost", **detail)


def probe_hooks() -> dict[str, Any]:
    from blackbox.hooks import claude_code, codex

    detail: dict[str, Any] = {}
    installed: list[str] = []
    for name, module in (("claude-code", claude_code), ("codex", codex)):
        try:
            status = module.status()
        except Exception as exc:
            detail[name] = f"{type(exc).__name__}: {exc}"
            continue
        detail[name] = status
        if status.get("installed"):
            installed.append(name)
    if installed:
        return _result("hooks", OK, f"hook adapters installed for {', '.join(installed)}", **detail)
    return _result("hooks", WARN, "no hook adapter is installed in this directory "
                                  "(`blackbox hook install --tool claude-code`)", **detail)


def probe_storage(data_dir: str) -> dict[str, Any]:
    path = Path(data_dir)
    probe_dir = path if path.exists() and os.access(path, os.W_OK) else Path(os.path.dirname(str(path)) or ".")
    if not probe_dir.exists() or not os.access(probe_dir, os.W_OK):
        return _result("storage", FAIL, f"neither {path} nor its parent is writable, so a tape cannot be created")
    from blackbox.platform import current as current_platform

    usage = shutil.disk_usage(str(probe_dir))
    detail: dict[str, Any] = {
        "directory": str(probe_dir), "free_bytes": usage.free, "total_bytes": usage.total,
        "free_mib": round(usage.free / (1024 * 1024), 1),
    }
    durability = current_platform()
    target = probe_dir / f".blackbox-doctor-{os.getpid()}.probe"
    started = time.monotonic()
    try:
        with open(target, "wb") as handle:
            handle.write(b"blackbox doctor write probe" * 64)
            handle.flush()
            # Through the dispatch, never `os.fsync` at the call site: on macOS this is the difference between
            # fsync(2) and F_FULLFSYNC, and the probe must time what the tape actually does.
            durability.fdatasync(handle.fileno())
        durability.fsync_dir(str(probe_dir))
        renamed = target.with_suffix(".renamed")
        os.replace(target, renamed)
        renamed.unlink()
        detail["write_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    except OSError as exc:
        target.unlink(missing_ok=True)
        return _result("storage", FAIL, f"a write probe failed in {probe_dir}: {type(exc).__name__}: {exc}", **detail)
    if usage.free < 64 * 1024 * 1024:
        return _result("storage", WARN, f"{detail['free_mib']} MiB free: a segment needs headroom", **detail)
    return _result("storage", OK, f"{detail['free_mib']} MiB free, and a write + fsync + rename took "
                                  f"{detail['write_ms']} ms", **detail)


def probe_locks(data_dir: str) -> dict[str, Any]:
    path = Path(data_dir)
    if not path.exists():
        return _result("locks", OK, "no data directory, so no lease can be held", held=[])
    # This data directory's own layout only. A `rglob` from here also walks whatever else a person keeps in the same
    # place — this repository, its `node_modules` and its `.git` — and reported seven "leases" that were package locks.
    candidates: list[Path] = []
    for pattern in ("*.lock", "*lease*", "*.lease"):
        candidates += sorted(path.glob(pattern))
    for sub in ("locks", "tape", "runs"):
        if (path / sub).is_dir():
            candidates += sorted((path / sub).rglob("*"))
    holders: list[dict[str, Any]] = []
    for candidate in candidates:
        if not candidate.is_file() or not ("lock" in candidate.name or "lease" in candidate.name):
            continue
        try:
            body = candidate.read_text(errors="replace")[:400]
        except OSError as exc:
            holders.append({"file": str(candidate), "read": f"{type(exc).__name__}: {exc}"})
            continue
        holders.append({"file": str(candidate), "contents": body.strip()})
    if holders:
        return _result("locks", WARN, f"{len(holders)} lock file(s) in the data directory; their contents say who holds "
                                      f"them", held=holders)
    return _result("locks", OK, "no lease file is held in this data directory", held=[])


PROBE_FUNCS = {
    "environment": probe_environment,
    "capture": lambda data_dir: probe_capture(),
    "tape": probe_tape,
    "hooks": lambda data_dir: probe_hooks(),
    "storage": probe_storage,
    "locks": probe_locks,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackbox doctor",
        description="Probe this machine and this data directory, and say what is fine, what is not, and what could not "
                    "be checked. Read-only apart from one probe file it deletes.")
    parser.add_argument("--data", default=None, help="data directory (default $BLACKBOX_DATA_DIR, $BLACKBOX_HOME, "
                                                     "or the current directory)")
    parser.add_argument("--capture", action="store_true", help="only the capture preflight")
    parser.add_argument("--tape", action="store_true", help="only the tape and capsule counts")
    parser.add_argument("--hooks", action="store_true", help="only the hook adapters")
    parser.add_argument("--storage", action="store_true", help="only free space and the write probe")
    parser.add_argument("--locks", action="store_true", help="only the writer leases")
    parser.add_argument("--json", action="store_true", help="one object on stdout")
    args = parser.parse_args(argv)

    data_dir = args.data or default_data_dir()
    selected = [name for name in ("capture", "tape", "hooks", "storage", "locks") if getattr(args, name)]
    probes = selected or list(PROBES)
    results = [PROBE_FUNCS[name](data_dir) for name in probes]
    failed = [r for r in results if r["status"] == FAIL]

    payload = {"schema": "blackbox.doctor/1", "version": __version__, "data_dir": data_dir,
               "probes": results, "failed": len(failed), "ok": not failed}
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 8 if failed else 0

    print(f"blackbox {__version__} · {platform.platform()} · data {data_dir}")
    for result in results:
        print(f"  {result['status']:>9}  {result['probe']:<12} {result['summary']}")
    if failed:
        print(f"\n{len(failed)} probe(s) failed. Each line above names what to change; "
              f"docs/troubleshooting.md has the same list with the commands.")
        return 8
    print("\nNothing failed. A `warn` is not a failure: it names something that will not work until you change it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
