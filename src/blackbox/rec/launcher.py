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
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.rec import autostart, correlate, discover, recovery
from blackbox.tape import head as head_mod
from blackbox.tape import segments
from blackbox.tape.keeper import Keeper
from blackbox.tape.writer import TapeWriter

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
    recovery_error: str | None = None
    proxy: dict[str, Any] = field(default_factory=dict)      # P3B-03: url, port, exports, adopted, rebinds
    hooks: dict[str, Any] = field(default_factory=dict)      # P3B-07: inbox, tape, events, frames, quarantined
    attach: dict[str, Any] = field(default_factory=dict)     # P3B-03: sdk startup reports, and every recovered vantage

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "argv": self.argv, "pid": self.pid, "exit_code": self.exit_code,
            "signaled": self.signaled, "duration_s": round(self.duration_s, 3),
            "stalled": self.stalled, "stall_ms": self.stall_ms, "stall_last_seq": self.stall_last_seq,
            "stall": self.stall_path, "stall_tb": self.dump_path, "writer_uuid": self.writer_uuid,
            "recovered": self.recovered, "recovery_error": self.recovery_error,
            "attach": self.attach,
        }


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
           sdk: bool = True, hooks: bool = True) -> RunResult:
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
    keeper = Keeper(writer).start()
    # one run id, derived from the tape, so the frames, the env the child sees and the recovery all agree
    run_id = run_id or recovery.run_id_for(data_dir, writer.writer_uuid.hex())
    child_env["BLACKBOX_RUN_ID"] = run_id
    child_env["BLACKBOX_HOME"] = data_dir
    # §11.9 step 1 names `BLACKBOX_DATA_DIR`; this build grew up on `BLACKBOX_HOME`. Both are set, with the same
    # value, so a child written against either spelling finds the run (D-044).
    child_env["BLACKBOX_DATA_DIR"] = data_dir
    child_env["BLACKBOX_STALL_MS"] = str(stall_ms)
    # A hook runs in the child's environment, so this is what tells `blackbox ingest` where *this* run's inbox is —
    # and therefore which tape the events will be folded into (P3B-07).
    if hooks:
        child_env["BLACKBOX_INBOX"] = os.path.join(data_dir, "inbox")
    # ---- the proxy (P3B-03): started before the child, because the child's environment names its port ----------
    proxy_thread, proxy_plan = (None, None)
    if proxy:
        proxy_thread, proxy_plan = _start_proxy(child_env, proxy_routes, proxy_port, proxy_receiver,
                                                retry_transmitted=proxy_retry_transmitted, force=proxy_force)
        child_env.update(proxy_plan.exports)
        child_env["BLACKBOX_PROXY_URL"] = proxy_thread.url

    # ---- the Python auto-import (P3B-03): the child's interpreter installs the SDK seam without the child knowing --
    startup: autostart.Startup | None = None
    if sdk and autostart.looks_like_python(argv):
        startup = autostart.arm(child_env, data_dir=data_dir)          # removed again when the child is gone
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "argv": list(argv), "pid": None,
                                               "stall_ms": stall_ms}))
    writer.flush()
    started = time.monotonic()
    # no shell: argv is the caller's list, and nothing here is built from a string
    folder = _InboxFolder(data_dir, run_id, out) if hooks else None
    if folder is not None:
        folder.start()
    try:
        child = subprocess.Popen(argv, env=child_env)  # noqa: S603
    except BaseException:                      # a command that does not exist must not leave a listener behind
        if proxy_thread is not None:
            proxy_thread.stop()
        if folder is not None:
            folder.close()
        if startup is not None:
            startup.close()
        keeper.stop()
        writer.close()
        raise
    result = RunResult(run_id=run_id, argv=list(argv), pid=child.pid, exit_code=0, stall_ms=stall_ms)
    watch = _Watch(data_dir, child.pid, stall_ms, out).start()
    try:
        raw = child.wait()
    finally:
        watch.stop()
        result.duration_s = time.monotonic() - started
        if startup is not None:
            startup.close()                    # the child is gone; the shim directory has no further use
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
            }
        finally:
            proxy_thread.stop()
    try:
        if folder is not None:
            folder.close()                   # the last hook events of a session belong inside the run
            result.hooks = folder.summary()
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
    reports = autostart.read_reports(data_dir)
    result.attach = {"sdk": {"armed": startup is not None,
                             "reason": None if startup is not None else
                             ("--no-sdk" if not sdk else "not a Python command"),
                             "reports": reports["startup"], "errors": reports["errors"]},
                     "vantages": []}
    if startup is not None:
        result.attach["sdk"]["directory"] = startup.directory
    if recover:
        try:
            result.attach["vantages"] = _recover_vantages(data_dir, writer.writer_uuid.hex(), run_id)
        except Exception as exc:                           # a vantage table is a report, never an exit-code change
            result.attach["recovery_error"] = f"{type(exc).__name__}: {exc}"
        own = next((row for row in result.attach["vantages"] if row.get("writer_uuid") == writer.writer_uuid.hex()),
                   None)
        if own is not None and "error" not in own:
            result.recovered = {"run_id": own["run_id"], "capsule_path": own["capsule_path"],
                                "capsule_root": own["capsule_root"], "run_status": own["run_status"],
                                "state": own.get("state"), "steps": own.get("steps")}
        elif own is not None:
            result.recovery_error = own["error"]
    if out:
        print(_summary_line(result), file=out)
    return result


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


def _recover_vantages(data_dir: str, own_uuid: str, run_id: str) -> list[dict[str, Any]]:
    """Recover every tape of this run — the SDK, proxy and hook vantages — children first, the launcher's last.

    §11.9 step 6 asks for "immediate recovery of every tape of this run", and P3B-03's done-when for "tape discovery
    for all three vantages at exit". The order is not cosmetic: a capsule's `links.json` is resolved when that capsule
    is sealed (D-038), so the launcher's own capsule — sealed last — can name the SDK and hook tapes it shares a run
    id with. Sealing the launcher first would produce a run whose own capsule links to nothing.

    A tape in the same data directory that belongs to **another** run is left alone and reported as skipped: a
    recorder may not swallow a neighbour's evidence. Failures are rows, not exceptions — the exit code was decided
    before this ran.
    """
    try:
        views = {view.writer_uuid: view for view in correlate.views(data_dir)}
    except Exception:                                  # a scan that fails means "no vantage names", not "no tapes"
        views = {}
    try:
        entries = discover.discover(data_dir)
    except Exception:
        return []
    children = [entry for entry in entries if entry.writer_uuid != own_uuid]
    mine = [entry for entry in entries if entry.writer_uuid == own_uuid]
    rows: list[dict[str, Any]] = []
    for entry in sorted(children, key=lambda e: (e.created_wall_ns, e.writer_uuid)) + mine:
        view = views.get(entry.writer_uuid)
        row: dict[str, Any] = {
            "writer_uuid": entry.writer_uuid,
            "vantage": view.vantage if view is not None else "unknown",
            "run_id": view.run_id if view is not None else None,
            "role": "launcher" if entry.writer_uuid == own_uuid else "child",
        }
        if entry.writer_uuid != own_uuid and (view is None or view.run_id != run_id):
            row["skipped"] = "another run's tape"
            rows.append(row)
            continue
        try:
            rec = recovery.recover(data_dir, entry.writer_uuid)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        else:
            row.update({"capsule_path": rec.capsule_path, "capsule_root": rec.capsule_root,
                        "run_status": rec.run_status, "state": rec.state, "steps": rec.steps_run})
        rows.append(row)
    return rows


def _summary_line(result: RunResult) -> str:
    bits = [f"run {result.run_id}", f"exit {result.exit_code}"]
    if result.proxy:
        bits.append(f"proxy {result.proxy['url']} ({len(result.proxy['exports'])} vars exported"
                    + (f", {len(result.proxy['adopted'])} adopted" if result.proxy["adopted"] else "") + ")")
    sdk = (result.attach or {}).get("sdk") or {}
    if sdk.get("armed"):
        seams = [report.get("seam") for report in sdk.get("reports") or []]
        captured = [seam for seam in seams if seam]
        if captured:
            bits.append(f"sdk {', '.join(sorted(set(captured)))}")
        elif sdk.get("errors"):
            bits.append(f"sdk failed ({len(sdk['errors'])} startup error(s), see {autostart.ATTACH_DIRNAME}/)")
        else:
            bits.append("sdk armed, no HTTP client imported")
    vantages = [row for row in (result.attach or {}).get("vantages") or [] if "skipped" not in row]
    if len(vantages) > 1:
        bits.append("vantages " + ", ".join(f"{row.get('vantage')} ({row.get('role')})"
                                            for row in sorted(vantages, key=lambda r: str(r.get("role")))))
    if result.hooks and result.hooks.get("tape"):
        bits.append(f"hooks {result.hooks['events']} events ({result.hooks['frames']} frames) → tape "
                    f"{str(result.hooks['tape'])[:8]}"
                    + (f", {len(result.hooks['quarantined'])} quarantined" if result.hooks["quarantined"] else ""))
    if result.signaled:
        bits.append(f"signal {result.signaled}")
    if result.stalled:
        bits.append(f"STALLED (no commit for {result.stall_ms} ms)")
    if result.recovered:
        bits.append(f"recovered {result.recovered.get('run_id')} → {result.recovered.get('capsule_path')} "
                    f"({result.recovered.get('run_status')}, root {str(result.recovered.get('capsule_root'))[:12]})")
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
                    help="point every base-URL variable at the proxy even where you already set it; the values it "
                         "replaced are printed and recorded, because moving your traffic silently is not an option")
    ap.add_argument("--no-sdk", action="store_true",
                    help="do not arm the child's Python auto-import; a child that imports no recorder is a child "
                         "with no SDK vantage, and the run says so instead of looking complete")
    ap.add_argument("--no-hooks", action="store_true",
                    help="record no hook vantage for this run: the inbox path is not exported and no folder runs")
    ap.add_argument("--proxy-port", type=int, default=0)
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
    result = launch(cmd, data_dir=args.data_dir, stall_ms=args.stall_ms, out=sys.stderr,
                    recover=not args.no_recover, proxy=not args.no_proxy, proxy_routes=routes,
                    proxy_port=args.proxy_port, proxy_retry_transmitted=args.proxy_retry_transmitted,
                    proxy_force=args.force_proxy, sdk=not args.no_sdk, hooks=not args.no_hooks)
    return result.exit_code


if __name__ == "__main__":                      # pragma: no cover - convenience
    raise SystemExit(main(sys.argv[1:]))
