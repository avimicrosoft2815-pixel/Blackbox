"""`blackbox run -- cmd`: spawn, watch for a stall, recover immediately, propagate the exit code (P2-11, P2-13).

Three promises, and each one is a test in `tests/test_launcher.py`:

* **the child's exit status is the launcher's exit status.** A recorder that swallows a non-zero exit changes what the
  user's script does, which is the one thing a fail-open recorder must never do. A child killed by signal *n* is
  reported as `128 + n`, the same convention a shell uses.
* **a stall is detected and dumped, not discovered later.** While the child runs, the launcher polls the committed
  head. If the head does not move for `stall_ms`, it writes `stall.json` (what it saw: pid, last seq, elapsed) and,
  on POSIX, sends `SIGUSR1` to the child. `api.install()` registers `faulthandler` on that signal, so the child
  writes every thread's stack to `stall_tb.txt` beside `stall.json`. On Windows there is no `SIGUSR1` and no
  `faulthandler` registration, so the launcher documents `stall.json` only — it does not pretend to have stacks.
* **the run is recovered the moment the child exits**, including when it was killed. That is why the plan's
  acceptance line is `blackbox run -- sh -c 'kill -9 $$'` followed by a recovered run on the next line: recovery is
  not a separate command the user has to remember.

The launcher never raises into the child's exit path: a failure to recover is reported, and the exit code still wins.

**The Python auto-import (§11.9 step 4)** is the other half of "universal attach": a Python child gets a temporary
directory on its `PYTHONPATH` holding a `sitecustomize.py`, which `site` imports before `__main__`, so `install()`
happens without the application importing anything. (The plan calls this "a `.pth` in a temp site dir"; a `.pth` on
`PYTHONPATH` is never read — measured, see `cap_sdk/autoimport.py` — so the mechanism is `sitecustomize`, the same one
`opentelemetry-instrument` uses.) Two properties are load-bearing and are tested by name: the directory is **temporary**
(created per run, deleted when the child exits — nothing is written into `site-packages` or a user's `~/.local/lib`), and
it is **only** added for a Python command, because a `sitecustomize` on a shell child's `PYTHONPATH` is a file nothing
reads and a variable nobody asked for.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.rec import discover, recovery
from blackbox.tape import head as head_mod
from blackbox.tape import segments
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter
from blackbox.trigger.engine import Engine
from blackbox.trigger.rules import parse_rules

STALL_FILE = "stall.json"
STALL_TXT = "stall_tb.txt"
POLL_S = 0.02


@dataclass
class RunResult:
    run_id: str
    argv: list[str]
    pid: int
    exit_code: int
    signaled: int | None = None
    duration_s: float = 0.0
    stalled: bool = False
    stall_ms: int = 0
    stall_last_seq: int | None = None
    stall_path: str | None = None
    dump_path: str | None = None
    writer_uuid: str = ""
    recovered: dict[str, Any] = field(default_factory=dict)
    capsules: list[dict[str, Any]] = field(default_factory=list)   # P3B-03: every tape of this run, with its role
    recovery_error: str | None = None
    proxy: dict[str, Any] = field(default_factory=dict)      # P3B-03: url, port, exports, adopted, rebinds
    captured: dict[str, Any] = field(default_factory=dict)   # B2: what the capsules of this run actually hold
    sdk: dict[str, Any] = field(default_factory=dict)        # P3B-03: the temp .pth, what it is, whether it was used
    hooks: dict[str, Any] = field(default_factory=dict)      # P3B-07: inbox, tape, events, frames, quarantined
    triggers: dict[str, Any] = field(default_factory=dict)   # P8-10: rules, hits, suppressed, errors, disabled

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "argv": self.argv, "pid": self.pid, "exit_code": self.exit_code,
            "signaled": self.signaled, "duration_s": round(self.duration_s, 3),
            "stalled": self.stalled, "stall_ms": self.stall_ms, "stall_last_seq": self.stall_last_seq,
            "stall": self.stall_path, "stall_tb": self.dump_path, "writer_uuid": self.writer_uuid,
            "recovered": self.recovered, "recovery_error": self.recovery_error,
            "hooks": self.hooks, "triggers": self.triggers,
        }


def _on_freeze_deadline(writer: Any) -> Any:
    """What a `stop_delay` deadline does: record that it passed. Sealing mid-run is not implemented (see docs)."""
    from blackbox.codec import cbor as _cbor

    def _apply(activity: Any) -> None:
        with contextlib.suppress(Exception):     # a trigger must never be able to hurt the app or the tape
            writer.append(C.KIND_MARK, _cbor.encode({
                "name": "freeze_due", "rule": activity.rule, "fingerprint": activity.fingerprint,
                "note": "the run is sealed once, at the end; this build does not seal a second capsule here"}),
                priority=2)
    return _apply


def _newest_tape_head(data_dir: str) -> tuple[str | None, int | None]:
    """The newest tape's head seq, or (None, None) when there is no tape yet."""
    try:
        entries = discover.discover(data_dir)
    except OSError:
        return None, None
    if not entries:
        return None, None
    writer = max(entries, key=lambda e: (e.created_wall_ns, e.writer_uuid)).writer_uuid
    tape_dir = os.path.join(data_dir, "tape", writer)
    head = head_mod.read_best(os.path.join(tape_dir, "head"))
    return writer, (head.head_seq if head is not None else None)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


class _Watch:
    """The stall watchdog: one thread, one counter, no assumption that the child records anything at all."""

    def __init__(self, data_dir: str, child_pid: int, stall_ms: int, out: IO[str] | None) -> None:
        self.data_dir = data_dir
        self.child_pid = child_pid
        self.stall_ms = stall_ms
        self.out = out
        self.stalled = False
        self.last_seq: int | None = None
        self.stall_path: str | None = None
        self.dump_path: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="blackbox-stall-watch", daemon=True)

    def start(self) -> _Watch:
        if self.stall_ms > 0:
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        last_change = time.monotonic()
        previous: int | None = None
        while not self._stop.wait(POLL_S):
            _writer, seq = _newest_tape_head(self.data_dir)
            self.last_seq = seq
            if seq != previous:
                previous = seq
                last_change = time.monotonic()
                continue
            idle_ms = (time.monotonic() - last_change) * 1000.0
            if idle_ms >= self.stall_ms:
                self._fire(seq, idle_ms)
                return

    def _fire(self, seq: int | None, idle_ms: float) -> None:
        self.stalled = True
        self.stall_path = os.path.join(self.data_dir, STALL_FILE)
        payload = {
            "pid": self.child_pid, "last_seq": seq, "stall_ms": self.stall_ms,
            "idle_ms": round(idle_ms, 1), "wall_ns": time.time_ns(), "mono_ns": time.monotonic_ns(),
            "signal": "SIGUSR1" if hasattr(signal, "SIGUSR1") else None,
        }
        try:
            _write_json(self.stall_path, payload)
        except OSError as exc:                      # never let the watchdog break the run
            if self.out:
                print(f"blackbox: could not write {self.stall_path}: {exc}", file=self.out)
            return
        if self.out:
            where = "stall.json"
            if hasattr(signal, "SIGUSR1"):
                self.dump_path = os.path.join(self.data_dir, STALL_TXT)
                try:
                    os.kill(self.child_pid, signal.SIGUSR1)
                    where = "stall.json + SIGUSR1 (a child that called blackbox.api.install() writes stall_tb.txt)"
                except OSError as exc:
                    where = f"stall.json (SIGUSR1 failed: {exc}); no stacks"
            print(f"blackbox: no commit from pid {self.child_pid} for {idle_ms:.0f} ms "
                  f"(stall_ms {self.stall_ms}, last seq {seq}) — {where}", file=self.out)


class _InboxFolder:
    """Folds a coding agent's hook inbox into its **own** tape while the child runs (P3B-07, §11.9 step 6).

    Three decisions, and each one is a test:

    * **Its own tape, and its own vantage.** §11.9 step 6 lists "every tape of this run (SDK, proxy, hook inbox)", and
      §11.10 correlates them afterwards. Folding hook events into the launcher's tape would work and would be wrong:
      the hook adapter is a vantage point, it says so in `meta.vantage = 3`, and a capsule that cannot tell a hook's
      `PreToolUse` from the launcher's own frames has lost the distinction the whole design rests on. The tape carries
      the **same `run_id`** as the launcher's, which is exactly what makes the `exact` link class possible later.
    * **Lazily opened.** A run that fires no hooks leaves no empty tape behind. An empty tape is noise, and "no hooks
      happened" is already the absence of frames.
    * **Every 250 ms, and once more before `RUN_END`.** The interval is the plan's; the final fold is what makes the
      last events of a session — the ones a `SessionEnd` hook fires — land in the tape even when the child exits the
      instant after writing them. Folding never raises into the child's exit path: a failure is counted and reported.
    """

    def __init__(self, data_dir: str, run_id: str, out: IO[str] | None = None, interval_s: float = 0.25) -> None:
        self.data_dir = data_dir
        self.inbox_dir = os.path.join(data_dir, "inbox")
        self.run_id = run_id
        self.out = out
        self.interval_s = interval_s
        self.events = 0
        self.frames = 0
        self.files = 0
        self.quarantined: list[str] = []
        self.errors: list[str] = []
        self.tape: str | None = None
        self._writer: TapeWriter | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="blackbox-inbox-fold", daemon=True)

    # -- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> _InboxFolder:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            with contextlib.suppress(Exception):
                self.fold_once()

    # -- work ----------------------------------------------------------------------------------------------------
    def _open(self) -> TapeWriter | None:
        if self._writer is not None:
            return self._writer
        from blackbox.hooks import inbox as inbox_mod

        if not inbox_mod.pending(self.inbox_dir):
            return None
        writer = TapeWriter(self.data_dir, vantage=3).open()      # 3 = hook-inbox, docs/format.md 0x03C
        writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": self.run_id, "vantage": "hook",
                                                   "inbox": self.inbox_dir}))
        self._writer = writer
        self.tape = writer.writer_uuid.hex()
        return writer

    def fold_once(self) -> int:
        """Fold everything pending. Returns how many entries were folded; never raises."""
        from blackbox.hooks import inbox as inbox_mod

        try:
            writer = self._open()
            if writer is None:
                return 0
            result = inbox_mod.fold(self.inbox_dir, writer)
        except Exception as exc:                       # a hook folder must never cost the run
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return 0
        self.files += result.files
        self.frames += result.frames
        self.events += result.files
        self.quarantined.extend(result.quarantined)
        return result.files

    def close(self) -> None:
        """One last fold, then `RUN_END` on the hook tape — the exit path, in that order."""
        self.stop()
        self.fold_once()
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": self.run_id, "reason": "child_exited",
                                                     "files": self.files, "frames": self.frames,
                                                     "quarantined": len(self.quarantined)}))
            writer.flush()
        finally:
            writer.close()

    def summary(self) -> dict[str, Any]:
        return {"inbox": self.inbox_dir, "tape": self.tape, "events": self.events, "frames": self.frames,
                "quarantined": list(self.quarantined), "errors": list(self.errors)}


def launch(argv: list[str], *, data_dir: str | None = None, stall_ms: int = 5_000,
           env: dict[str, str] | None = None, out: IO[str] | None = None, recover: bool = True,
           run_id: str | None = None, proxy: bool = False, proxy_routes: Any = None,
           proxy_port: int = 0, proxy_receiver: Any = None,
           proxy_retry_transmitted: bool = False, proxy_force: bool = False,
           sdk: bool = True, hooks: bool = True, triggers: list[dict[str, Any]] | None = None) -> RunResult:
    """Run `argv` under the recorder, watch for a stall, then recover the tape that the child left behind."""
    if not argv:
        raise ValueError("launch() needs a command")
    data_dir = os.path.abspath(data_dir or os.environ.get("BLACKBOX_HOME") or ".")
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    child_env = dict(os.environ if env is None else env)
    # The launcher is the recorder host: it owns the tape, so the run exists even when the child records nothing.
    # That is what makes the plan's acceptance line work — `sh -c 'kill -9 $$'` still leaves a tape, a RUN_END and
    # therefore a recovered run, instead of an empty directory and a shrug.
    writer = TapeWriter(data_dir, mode="process").open()
    # §10.5: rules from `[[trigger]]` in blackbox.toml. The engine is fed by whoever sees a metric and decides in the
    # keeper's thread; `on_trigger` records that a freeze deadline passed. What this build does *not* do is seal a second
    # capsule at that deadline — the run is sealed once, at the end — so a `freeze` names the events to look at rather
    # than producing a superset capsule mid-run. That is stated in `docs/triggers.md` rather than implied here.
    trigger_rules = parse_rules(triggers) if triggers else []
    engine = (Engine(trigger_rules, store=os.path.join(data_dir, "triggers.json")) if trigger_rules else None)
    keeper = Keeper(writer, triggers=engine, on_trigger=_on_freeze_deadline(writer)).start()
    # one run id, derived from the tape, so the frames, the env the child sees and the recovery all agree
    run_id = run_id or recovery.run_id_for(data_dir, writer.writer_uuid.hex())
    child_env["BLACKBOX_RUN_ID"] = run_id
    child_env["BLACKBOX_HOME"] = data_dir
    child_env["BLACKBOX_STALL_MS"] = str(stall_ms)
    # A hook runs in the child's environment, so this is what tells `blackbox ingest` where *this* run's inbox is —
    # and therefore which tape the events will be folded into (P3B-07). `--no-hooks` removes the variable rather than
    # leaving it and ignoring the files: a hook that still writes an inbox nobody folds is a directory that grows.
    inbox_dir = os.path.join(data_dir, "inbox")
    if hooks:
        child_env["BLACKBOX_INBOX"] = inbox_dir
    # ---- the proxy (P3B-03): started before the child, because the child's environment names its port ----------
    proxy_thread, proxy_plan = (None, None)
    if proxy:
        # The proxy is a vantage of its own (D-037): its frames belong on the launcher's tape, which is the tape this
        # process owns. Without a receiver the listener forwards traffic and records *nothing*, so `nodes` stayed at 0
        # and a run through the CLI had no model request in any capsule — traffic that passed silently. The default is
        # here rather than in the CLI so that every caller of `launch(proxy=True)` gets a recording proxy, and the
        # explicit `proxy_receiver` stays an override for tests that want their own sink.
        receiver = proxy_receiver if proxy_receiver is not None else _default_proxy_receiver(writer)
        proxy_thread, proxy_plan = _start_proxy(child_env, proxy_routes, proxy_port, receiver,
                                               retry_transmitted=proxy_retry_transmitted, force=proxy_force)
        child_env.update(proxy_plan.exports)
        child_env["BLACKBOX_PROXY_URL"] = proxy_thread.url
        # Forcing is never silent: every variable the flag took away is named, with the value it had, because the
        # user's next question is "which of my settings did this change".
        if proxy_plan.overridden and out:
            for var, was in sorted(proxy_plan.overridden.items()):
                print(f"blackbox: --force-proxy points {var} at the proxy (was {was}); the run's traffic is recorded",
                      file=out)
    # ---- the Python auto-import (P3B-03): a temporary .pth, removed when the child exits --------------------------
    sdk_record: dict[str, Any] = {"enabled": False, "reason": "--no-sdk" if not sdk else "not a Python command"}
    if sdk:
        sdk_record = _install_sdk(child_env, argv, out)

    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "argv": list(argv), "pid": None,
                                               "stall_ms": stall_ms}))
    writer.flush()
    started = time.monotonic()
    # no shell: argv is the caller's list, and nothing here is built from a string
    folder = _InboxFolder(data_dir, run_id, out)
    if hooks:
        folder.start()
    try:
        child = subprocess.Popen(argv, env=child_env)  # noqa: S603
    except BaseException:                      # a command that does not exist must not leave a listener behind
        if proxy_thread is not None:
            proxy_thread.stop()
        cleanup_sdk(sdk_record)
        folder.close()
        keeper.stop()
        writer.close()
        raise
    result = RunResult(run_id=run_id, argv=list(argv), pid=child.pid, exit_code=0, stall_ms=stall_ms)
    result.sdk = sdk_record
    watch = _Watch(data_dir, child.pid, stall_ms, out).start()
    try:
        raw = child.wait()
    finally:
        watch.stop()
        result.duration_s = time.monotonic() - started
    if raw < 0:                                            # killed by signal: 128 + n, the shell's convention
        result.signaled = -raw
        result.exit_code = 128 - raw
    else:
        result.exit_code = raw
    if proxy_thread is not None and proxy_plan is not None:
        try:
            server = proxy_thread.server
            result.proxy = {
                "url": proxy_thread.url,
                "port": proxy_thread.port,
                "exports": dict(sorted(proxy_plan.exports.items())),
                "adopted": dict(sorted(proxy_plan.adopted.items())),
                "overridden": dict(sorted(proxy_plan.overridden.items())),
                "rebinds": list(proxy_thread.rebinds),
                "retry_transmitted": proxy_thread.retry_transmitted,
                "port_changed": proxy_thread.port_changed,
                "stats": server.stats.as_dict() if server is not None else {},
                # Defect 108: the loop's own callback exceptions. On the Windows sample one of these existed only in a
                # job log this sandbox cannot read, so the run kept no record of it. Bounded in the recorder; repeated
                # here as a count plus the first few, because a summary that carries 256 tracebacks is not readable.
                "loop_error_count": len(proxy_thread.loop_errors),
                "loop_errors": list(proxy_thread.loop_errors[:8]),
            }
        finally:
            proxy_thread.stop()
    try:
        cleanup_sdk(sdk_record)              # the child is gone; the temporary site dir goes with it
        folder.close()                       # the last hook events of a session belong inside the run
        result.hooks = folder.summary()
        if not hooks:
            result.hooks = {"inbox": None, "tape": None, "events": 0, "frames": 0, "quarantined": [], "errors": [],
                            "enabled": False, "reason": "--no-hooks"}
        # §10.5: the launcher evaluates `exit_code` and `stall_ms` from what it alone can see, then lets the keeper write
        # what the engine decided. This is the backstop: a starved keeper cannot blind the engine to a run's own ending.
        if engine is not None:
            engine.observe_all("exit_code", float(result.exit_code))
            engine.observe_all("stall_ms", float(result.duration_s * 1000.0) if watch.stalled else 0.0)
            keeper.evaluate_triggers(force=True)
        # The proxy's own record goes into the tape, not just into this process's memory (defect 108). Until this
        # frame existed, `rebinds` and `loop_errors` died with the launcher: a user who hit a slow run could not send
        # anything a maintainer could read. It is a MARK, which every reader already knows and which changes no derived
        # part, and it is written before RUN_END because the run is sealed after that.
        if result.proxy:
            writer.append(C.KIND_MARK, cbor.encode({
                "name": "proxy_loop",
                "rebinds_ms": [round(value, 3) for value in result.proxy.get("rebinds") or []],
                "port_changed": bool(result.proxy.get("port_changed")),
                "loop_error_count": int(result.proxy.get("loop_error_count") or 0),
                "loop_errors": result.proxy.get("loop_errors") or [],
                "requests": int((result.proxy.get("stats") or {}).get("requests") or 0),
            }), priority=2)
        writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": run_id, "exit_code": result.exit_code,
                                                 "signaled": result.signaled, "stalled": watch.stalled,
                                                 "duration_ms": int(result.duration_s * 1000)}))
        writer.flush()
    finally:
        keeper.stop()
        writer.close()
    result.stalled = watch.stalled
    result.stall_last_seq = watch.last_seq
    result.stall_path = watch.stall_path
    result.dump_path = watch.dump_path
    result.writer_uuid = writer.writer_uuid.hex()
    result.triggers = keeper.stats().get("triggers") or {"rules": []}
    if recover:
        try:
            primary = writer.writer_uuid.hex()
            capsules = _recover_run(data_dir, run_id, primary)
            result.capsules = capsules
            first = next((c for c in capsules if c["writer_uuid"] == primary), None)
            if first is not None and "error" not in first:
                result.recovered = {"run_id": first["run_id"], "capsule_path": first["capsule_path"],
                                    "capsule_root": first["capsule_root"], "run_status": first["run_status"],
                                    "state": first["state"], "capsules": len(capsules),
                                    "roles": [c["role"] for c in capsules]}
            elif capsules and "error" in capsules[0]:
                result.recovery_error = capsules[0]["error"]
        except Exception as exc:                           # recovery is a report, never an exit-code change
            result.recovery_error = f"{type(exc).__name__}: {exc}"
    if recover and result.capsules:
        result.captured = _capture_totals(result.capsules)
    if out:
        print(_summary_line(result), file=out)
        if recover and result.capsules and result.proxy.get("url") and result.captured.get("model_calls") == 0:
            # B2 in docs/RELEASE-BLOCKERS.md: the run reports `recovered`, lists capsules, exits 0 — and says nothing
            # about the fact that none of them holds a request. The commonest cause is not a bug in the recorder: a base
            # URL the application sets is adopted as an upstream rather than overwritten, so the traffic never arrives.
            print(_empty_capture_warning(result), file=out)
        limit = _limit_warning(result)
        if limit is not None:
            print(limit, file=out)
    return result


def _capture_totals(capsules: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum what the run's capsules say they captured: model calls, events, and how many capsules could be read.

    Read from each manifest rather than inferred from the proxy's own counters: `ls` would show these capsules to the
    user tomorrow, and a run report that disagrees with them is the kind of second answer this project refuses.
    """
    calls = events = unreadable = 0
    for capsule in capsules:
        path = capsule.get("capsule_path")
        if not path:
            unreadable += 1
            continue
        try:
            from blackbox.cap.format import read_capsule

            manifest = read_capsule(str(path), max_bytes=32 << 20).manifest
            calls += int((manifest.get("capture") or {}).get("model_calls") or 0)
            events += int((manifest.get("counts") or {}).get("events") or 0)
        except Exception:
            unreadable += 1
    return {"model_calls": calls, "events": events, "capsules": len(capsules), "unreadable": unreadable}


def _limit_warning(result: RunResult) -> str | None:
    """Say what happened at the proxy's ceiling, and which of the two things it was (B3, `docs/RELEASE-BLOCKERS.md`).

    A **delay** and a **rejection** are different facts and a user cannot act on them the same way, so they are named
    separately and never added together: a request that waited for a slot was still answered, and a request the recorder
    refused never reached a provider and came back as a `503`. The ceilings in the sentence are read from the proxy's own
    `stats.caps`, so a host that had to shrink them prints the numbers it actually used rather than the documented pair.
    """
    stats = (result.proxy or {}).get("stats") or {}
    caps = stats.get("caps") or {}
    refused = int(stats.get("refused") or 0)
    queued = int(stats.get("queued") or 0)
    if not refused and not queued:
        return None
    in_flight = caps.get("max_inflight")
    connections = caps.get("max_connections")
    budget = caps.get("fd_budget")
    parts: list[str] = []
    if queued:
        parts.append("delay, nothing lost: {n} request{s} waited for one of the recorder's {cap} in-flight slots, the "
                     "longest {ms:.0f} ms; every one of them was passed through".format(
                         n=queued, s="" if queued == 1 else "s", cap=in_flight or "?",
                         ms=float(stats.get("max_queue_wait_ms") or 0.0)))
    if refused:
        reasons = stats.get("refused_reasons") or {}
        detail = ", ".join(f"{name}: {count}" for name, count in sorted(reasons.items())) or "no reason recorded"
        parts.append("rejection: the recorder answered {n} request{s} itself instead of passing {them} through "
                     "({detail}); those never reached a provider, and the client was given a 503 naming the ceiling, "
                     "so a retry is the action".format(
                         n=refused, s="" if refused == 1 else "s", them="it" if refused == 1 else "them", detail=detail))
    if in_flight and connections and budget:
        parts.append(f"the ceilings are {in_flight} in flight and {connections} connections, derived from this host's "
                     f"{budget}-descriptor budget rather than set by hand")
    elif in_flight and connections:
        parts.append(f"the ceilings are {in_flight} in flight and {connections} connections; this platform publishes no "
                     f"descriptor budget, so they are policy numbers and the 503 at them is what bounds the process")
    return "blackbox: " + "; ".join(parts) + "."


def _empty_capture_warning(result: RunResult) -> str:
    count = result.captured.get("capsules", len(result.capsules))
    # A possible cause, never a finding: this recorder writes the capsule it is given and cannot see the child's network.
    # A command that talks to no model API produces exactly this line, and so does a call that never left the process
    # (a mock client, an SDK that ignores the variable). Only the first of the causes below is a recorder problem, and the
    # sentence is written so a reader can tell them apart instead of being told which one happened.
    return ("blackbox: this run recorded no model call — the {n} capsule{s} of run {run} {hold} no request and no "
            "response. That is not a failure by itself: a command that talks to no model API is exactly this. If this "
            "one was meant to talk to a model API, the base URL is one possible cause (a base URL your environment or "
            "code sets is adopted as an upstream rather than overwritten, so the traffic never arrives), and `blackbox "
            "run --force-proxy` points every base-URL variable at the recorder and names each one it displaces. A call "
            "that never left the process — a mock, or a client that ignores the variable — would look the same from "
            "here. This line describes the run, not a verdict on it."
            ).format(n=count, s="" if count == 1 else "s", hold="holds" if count == 1 else "hold",
                     run=result.run_id)


#: `python`, `python3`, `python3.11`, `pythonw` — and nothing else. The first draft ended in `[a-z]*`, which also
#: accepted `pythonista` (a text editor): the launcher would have put a `sitecustomize` on a stranger's `PYTHONPATH` and
#: reported `sdk: enabled` for a child that never reads it. `test_the_python_command_test_itself_is_platform_neutral`
#: is what caught it.
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)*)?w?$")
#: Windows' launcher, `py.exe` / `pyw.exe`. It is a Python command in the sense that matters — the child is a Python
#: interpreter — but only when the name actually resolves on this machine, never as a bare string.
_PY_LAUNCHER = re.compile(r"^pyw?$")


def _is_python_command(argv: list[str]) -> tuple[bool, str]:
    """Is this child a Python interpreter, and why? No guessing from a shebang: the *resolved* name is the fact.

    `argv[0]` is looked up through `PATH` exactly as `Popen` will look it up, then compared by base name. A `.py` file
    is not a Python command in this sense (it may well be run by something else); the interpreter is.
    """
    name = argv[0] if argv else ""
    resolved = shutil.which(name) or name
    base = os.path.basename(resolved).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if _PYTHON_NAME.match(base):
        return True, resolved
    if _PY_LAUNCHER.match(base) and os.name == "nt" and shutil.which(name):
        return True, resolved
    return False, resolved


def _package_root() -> str | None:
    """The directory that has `blackbox/` inside it, so the child's interpreter can import us at all."""
    try:
        import blackbox

        return os.path.dirname(os.path.dirname(os.path.abspath(blackbox.__file__)))
    except Exception:                                       # pragma: no cover - the launcher itself imported it
        return None


def _tape_run_id(data_dir: str, writer_uuid: str) -> str | None:
    """The run id a tape names in its own `RUN_START`, or `None` when it has none (or cannot be read)."""
    from blackbox.tape import scanner

    try:
        scan = scanner.scan(data_dir, writer_uuid)
    except Exception:
        return None
    for frame in scan.frames:
        if frame.get("kind") != C.KIND_RUN_START:
            continue
        try:
            body = cbor.decode_exact(frame.get("payload") or b"")
        except Exception:
            continue
        name = body.get("run_id") if isinstance(body, dict) else None
        return name if isinstance(name, str) and name else None
    return None


def _tape_role(data_dir: str, writer_uuid: str) -> str:
    """What a tape is, for the summary line: the SDK says so itself, the launcher's own has an `argv`."""
    from blackbox.tape import scanner

    try:
        scan = scanner.scan(data_dir, writer_uuid)
    except Exception:
        return "unknown"
    for frame in scan.frames:
        if frame.get("kind") != C.KIND_RUN_START:
            continue
        try:
            body = cbor.decode_exact(frame.get("payload") or b"")
        except Exception:
            return "unknown"
        if not isinstance(body, dict):
            return "unknown"
        if body.get("source") == "sdk":
            return "sdk"
        if body.get("vantage") == "hook":
            return "hook"
        return "launcher"
    return "launcher"


def _recover_run(data_dir: str, run_id: str, primary: str) -> list[dict[str, Any]]:
    """Recover **every** tape of this run (§11.9 step 6), the primary first, and report each one.

    One `blackbox run` is not one tape: the launcher writes its own boundary, a Python child's SDK writes a second,
    and the hook inbox a third. Recovering only the launcher's tape is why the child's frames were in no capsule at
    all, and it is the difference between "the run was recovered" and "the run was recovered, minus the part that did
    the work". Each tape keeps its own capsule (identity is per tape, `recovery.Recovery.run_id`), and the capsules
    are joined by `links.json`, which is where §11.10 puts a relation rather than in a tape.
    """
    try:
        entries = discover.discover(data_dir)
    except OSError:
        entries = []
    order = [primary] + [e.writer_uuid for e in entries if e.writer_uuid != primary]
    out: list[dict[str, Any]] = []
    for writer_uuid in order:
        if writer_uuid not in {e.writer_uuid for e in entries} and writer_uuid != primary:
            continue
        named = _tape_run_id(data_dir, writer_uuid)
        if writer_uuid != primary and named != run_id:
            continue                      # a tape from another run in the same data directory
        role = _tape_role(data_dir, writer_uuid)
        try:
            rec = recovery.recover(data_dir, writer_uuid)
        except Exception as exc:          # a tape that cannot be recovered is reported, not fatal
            out.append({"writer_uuid": writer_uuid, "role": role, "error": f"{type(exc).__name__}: {exc}"})
            continue
        out.append({"writer_uuid": writer_uuid, "role": role, "run_id": rec.run_id,
                    "capsule_path": rec.capsule_path, "capsule_root": rec.capsule_root,
                    "run_status": rec.run_status, "state": rec.state, "linked_run_id": named})
    return out


def _install_sdk(env: dict[str, str], argv: list[str], out: IO[str] | None) -> dict[str, Any]:
    """Prepend a temporary `.pth` site dir to the child's `PYTHONPATH`; return the record of what was done.

    The directory holds one file, `blackbox-autoload.pth`, whose single line is an `import`. `site` executes it during
    start-up, so the capture layer is installed before the application's first statement and the application's code
    is untouched. The caller is responsible for `cleanup_sdk()` — the launcher does it in a `finally`, so a child that
    is killed still leaves no directory behind.
    """
    from blackbox.cap_sdk import autoimport

    is_python, resolved = _is_python_command(argv)
    record: dict[str, Any] = {"enabled": False, "reason": "", "interpreter": resolved,
                              "is_python": is_python, "site_dir": None,
                              "mechanism": autoimport.MECHANISM, "file": autoimport.SITECUSTOMIZE_NAME}
    if not is_python:
        record["reason"] = "not a Python command"
        return record
    root = _package_root()
    if root is None:                                        # pragma: no cover
        record["reason"] = "the blackbox package could not be located"
        return record
    try:
        site_dir = tempfile.mkdtemp(prefix="blackbox-sdk-")
        with open(os.path.join(site_dir, autoimport.SITECUSTOMIZE_NAME), "w", encoding="utf-8") as fh:
            fh.write(autoimport.SITECUSTOMIZE_BODY)
    except OSError as exc:
        record["reason"] = f"{type(exc).__name__}: {exc}"
        return record
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([site_dir, root] + ([existing] if existing else []))
    env[autoimport.ENV_SDK] = "1"                          # the child may set it to 0 to opt out; --no-sdk is ours
    record.update({"enabled": True, "reason": "temporary sitecustomize on PYTHONPATH", "site_dir": site_dir,
                   "package_root": root})
    if out:
        print(f"blackbox: python child gets a temporary sitecustomize ({site_dir}); removed when it exits", file=out)
    return record


def cleanup_sdk(record: dict[str, Any]) -> None:
    """Remove the temporary site dir. Idempotent, quiet, and never raises into an exit path.

    `removed` reports what **happened**, not what was attempted. On Windows a directory that has just been written by
    another process can stay locked for a moment (the child's `__pycache__`, a virus scanner, a handle closing), so a
    single `rmtree(ignore_errors=True)` can leave the directory behind while the record says it is gone — which is the
    one thing a record of a cleanup must not do. Three attempts with a short pause, then the truth either way.
    """
    site_dir = record.get("site_dir")
    if not site_dir:
        return
    for attempt in range(3):
        shutil.rmtree(site_dir, ignore_errors=True)
        if not os.path.exists(site_dir):
            break
        if attempt < 2:
            time.sleep(0.05 * (attempt + 1))
    # `site_dir` is kept in the record on purpose: "where it was, and whether it is gone" is checkable afterwards,
    # while nulling it would leave a test only the launcher's word that a directory ever existed.
    record["removed"] = not os.path.exists(site_dir)
    if not record["removed"]:
        record["cleanup_error"] = f"{site_dir} still exists after three attempts"


def _default_proxy_receiver(writer: Any) -> Any:
    """A sink that turns the proxy's seam events into frames on the launcher's tape.

    `TapeWriter.emit` is lock-guarded, which matters because these calls arrive on the proxy's event-loop thread while
    the launcher emits `RUN_START`/`RUN_END` from this one — two threads, one frame chain.
    """
    from blackbox.cap_sdk.sink import SinkRecorder, TapeSink

    return SinkRecorder(TapeSink(writer.emit))


def _start_proxy(env: dict[str, str], routes: Any = None, port: int = 0, receiver: Any = None,
                 *, retry_transmitted: bool = False, force: bool = False) -> tuple[Any, Any]:
    """Build the table from the plan, start the listener, and return it with the plan that names its port (P3B-03).

    The plan is made **before** the listener exists because the table depends on it (a base URL the user already set
    becomes a route); the exports are filled in afterwards, from the port that was actually bound.
    """
    from blackbox.proxy.routes import Routes
    from blackbox.proxy.supervisor import BASE_URL_VARS, ProxyThread, plan_exports

    wanted = tuple(name for _var, name in BASE_URL_VARS)
    plan = plan_exports(env, force=force)
    table = routes or Routes.provider_defaults(names=wanted, default=plan.default_route or "openai")
    if plan.routes:
        table = table.extended(plan.routes, default=table.default or plan.default_route)
    thread = ProxyThread(table, receiver=receiver, port=port, retry_transmitted=retry_transmitted).start()
    return thread, plan_exports(env, thread.url, force=force)


def _summary_line(result: RunResult) -> str:
    bits = [f"run {result.run_id}", f"exit {result.exit_code}"]
    if result.proxy:
        bits.append(f"proxy {result.proxy['url']} ({len(result.proxy['exports'])} vars exported"
                    + (f", {len(result.proxy['adopted'])} adopted" if result.proxy["adopted"] else "") + ")")
        if result.proxy.get("loop_error_count"):
            # Says what happened and where it is kept, without naming a cause: what a callback raised is evidence, and
            # deciding whether it is the explanation belongs to a person reading it (defect 108).
            bits.append(f"the proxy loop raised {result.proxy['loop_error_count']} exception(s), "
                        f"recorded in the capsule as a proxy_loop mark")
    if result.sdk and result.sdk.get("enabled"):
        bits.append("sdk auto-import (sitecustomize)")
    if result.hooks and result.hooks.get("enabled") is False:
        bits.append("hooks off (--no-hooks)")
    if result.hooks and result.hooks.get("tape"):
        bits.append(f"hooks {result.hooks['events']} events ({result.hooks['frames']} frames) → tape "
                    f"{str(result.hooks['tape'])[:8]}"
                    + (f", {len(result.hooks['quarantined'])} quarantined" if result.hooks["quarantined"] else ""))
    if result.signaled:
        bits.append(f"signal {result.signaled}")
    if result.stalled:
        bits.append(f"STALLED (no commit for {result.stall_ms} ms)")
    if result.recovered:
        more = len(result.capsules) - 1
        bits.append(f"recovered {result.recovered.get('run_id')} → {result.recovered.get('capsule_path')} "
                    f"({result.recovered.get('run_status')}, root {str(result.recovered.get('capsule_root'))[:12]})"
                    + (f" and {more} more tape{'s' if more != 1 else ''} "
                       f"({', '.join(c['role'] for c in result.capsules[1:])})" if more > 0 else ""))
    elif result.recovery_error:
        bits.append(f"recovery failed: {result.recovery_error}")
    return "blackbox: " + " · ".join(bits)


def segment_count(data_dir: str) -> int:
    """Small helper for the CLI's one-line report: how many segments the child left."""
    entries = discover.discover(data_dir)
    total = 0
    for entry in entries:
        total += len(segments.list_segments(os.path.join(data_dir, "tape", entry.writer_uuid)))
    return total


def main(argv: list[str]) -> int:
    """`blackbox run [--stall-ms N] [--data-dir DIR] -- cmd ...`"""
    import argparse

    ap = argparse.ArgumentParser(prog="blackbox run", description="run a command under the recorder")
    ap.add_argument("--stall-ms", type=int, default=5_000)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--no-recover", action="store_true")
    ap.add_argument("--no-proxy", action="store_true", help="do not start the loopback proxy (P3B-03)")
    ap.add_argument("--force-proxy", action="store_true",
                    help="point every base-URL variable at the proxy even where the user already set one; each "
                         "variable it displaces is named on stderr and in the run result")
    ap.add_argument("--no-sdk", action="store_true",
                    help="do not add the temporary .pth auto-import for a Python child")
    ap.add_argument("--no-hooks", action="store_true",
                    help="do not export BLACKBOX_INBOX and do not fold a hook inbox for this run")
    ap.add_argument("--proxy-port", type=int, default=0)
    ap.add_argument("--json", action="store_true",
                    help="print the run result as one JSON object on stdout after the child exits "
                         "(the child's own stdout is untouched; the human summary still goes to stderr)")
    ap.add_argument("--proxy-retry-transmitted", action="store_true",
                    help="retry GET/HEAD/OPTIONS once when the upstream dies after the request was sent; some "
                         "upstreams give a GET side effects, so this is off by default")
    ap.add_argument("--upstream", action="append", default=[], metavar="NAME=URL[@/prefix]",
                    help="add or replace an upstream route; the table is the proxy's egress allowlist")
    ap.add_argument("--insecure-upstream", action="append", default=[], metavar="NAME",
                    help="do not verify TLS for this upstream (local servers only)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("blackbox run: a command is required (blackbox run -- cmd ...)", file=sys.stderr)
        return 2
    routes = None
    if args.upstream:
        from blackbox.proxy.routes import Routes

        routes = Routes.from_specs(args.upstream, unverified=tuple(args.insecure_upstream), default=None)
    try:                                     # `[[trigger]]` rules come from config; a bad one is refused by name
        from blackbox.config import load_config

        trigger_tables = list(load_config().triggers)
    except Exception as exc:
        print(f"blackbox run: configuration refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    result = launch(cmd, data_dir=args.data_dir, stall_ms=args.stall_ms, out=sys.stderr,
                    recover=not args.no_recover, proxy=not args.no_proxy, proxy_routes=routes,
                    proxy_port=args.proxy_port, proxy_retry_transmitted=args.proxy_retry_transmitted,
                    proxy_force=args.force_proxy, sdk=not args.no_sdk, hooks=not args.no_hooks,
                    triggers=trigger_tables)
    if args.json:
        import json

        print(json.dumps(result.as_dict(), sort_keys=True), flush=True)   # one line: the child owns the rest of stdout
    return result.exit_code


if __name__ == "__main__":                      # pragma: no cover - convenience
    raise SystemExit(main(sys.argv[1:]))
