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
from blackbox.rec import discover, recovery
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "argv": self.argv, "pid": self.pid, "exit_code": self.exit_code,
            "signaled": self.signaled, "duration_s": round(self.duration_s, 3),
            "stalled": self.stalled, "stall_ms": self.stall_ms, "stall_last_seq": self.stall_last_seq,
            "stall": self.stall_path, "stall_tb": self.dump_path, "writer_uuid": self.writer_uuid,
            "recovered": self.recovered, "recovery_error": self.recovery_error,
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


def launch(argv: list[str], *, data_dir: str | None = None, stall_ms: int = 5_000,
           env: dict[str, str] | None = None, out: IO[str] | None = None, recover: bool = True,
           run_id: str | None = None, proxy: bool = False, proxy_routes: Any = None,
           proxy_port: int = 0, proxy_receiver: Any = None,
           proxy_retry_transmitted: bool = False) -> RunResult:
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
    child_env["BLACKBOX_STALL_MS"] = str(stall_ms)
    # ---- the proxy (P3B-03): started before the child, because the child's environment names its port ----------
    proxy_thread, proxy_plan = (None, None)
    if proxy:
        proxy_thread, proxy_plan = _start_proxy(child_env, proxy_routes, proxy_port, proxy_receiver,
                                                retry_transmitted=proxy_retry_transmitted)
        child_env.update(proxy_plan.exports)
        child_env["BLACKBOX_PROXY_URL"] = proxy_thread.url

    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": run_id, "argv": list(argv), "pid": None,
                                               "stall_ms": stall_ms}))
    writer.flush()
    started = time.monotonic()
    # no shell: argv is the caller's list, and nothing here is built from a string
    try:
        child = subprocess.Popen(argv, env=child_env)  # noqa: S603
    except BaseException:                      # a command that does not exist must not leave a listener behind
        if proxy_thread is not None:
            proxy_thread.stop()
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
                "rebinds": list(proxy_thread.rebinds),
                "retry_transmitted": proxy_thread.retry_transmitted,
                "port_changed": proxy_thread.port_changed,
                "stats": server.stats.as_dict() if server is not None else {},
            }
        finally:
            proxy_thread.stop()
    try:
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
    if recover:
        try:
            rec = recovery.recover(data_dir, writer.writer_uuid.hex())
            result.recovered = {"run_id": rec.run_id, "capsule_path": rec.capsule_path,
                                "capsule_root": rec.capsule_root, "run_status": rec.run_status,
                                "state": rec.state, "steps": rec.steps_run}
        except Exception as exc:                           # recovery is a report, never an exit-code change
            result.recovery_error = f"{type(exc).__name__}: {exc}"
    if out:
        print(_summary_line(result), file=out)
    return result


def _start_proxy(env: dict[str, str], routes: Any = None, port: int = 0, receiver: Any = None,
                 *, retry_transmitted: bool = False) -> tuple[Any, Any]:
    """Build the table from the plan, start the listener, and return it with the plan that names its port (P3B-03).

    The plan is made **before** the listener exists because the table depends on it (a base URL the user already set
    becomes a route); the exports are filled in afterwards, from the port that was actually bound.
    """
    from blackbox.proxy.routes import Routes
    from blackbox.proxy.supervisor import BASE_URL_VARS, ProxyThread, plan_exports

    wanted = tuple(name for _var, name in BASE_URL_VARS)
    plan = plan_exports(env)
    table = routes or Routes.provider_defaults(names=wanted, default=plan.default_route or "openai")
    if plan.routes:
        table = table.extended(plan.routes, default=table.default or plan.default_route)
    thread = ProxyThread(table, receiver=receiver, port=port, retry_transmitted=retry_transmitted).start()
    return thread, plan_exports(env, thread.url)


def _summary_line(result: RunResult) -> str:
    bits = [f"run {result.run_id}", f"exit {result.exit_code}"]
    if result.proxy:
        bits.append(f"proxy {result.proxy['url']} ({len(result.proxy['exports'])} vars exported"
                    + (f", {len(result.proxy['adopted'])} adopted" if result.proxy["adopted"] else "") + ")")
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
                    proxy_port=args.proxy_port, proxy_retry_transmitted=args.proxy_retry_transmitted)
    return result.exit_code


if __name__ == "__main__":                      # pragma: no cover - convenience
    raise SystemExit(main(sys.argv[1:]))
