"""The child's-eye clock for a rebind: when a request stopped being served, and when one was served again.

The question §11.8 asks is not "how long did `rebind()` take" and not "when did `close()` return". It is: **during
which interval could a request not be served**. Every other clock is a proxy for that one, and each of them has been
wrong here at least once:

* `rebind_ms` (the old row) measures our own work and calls the queueing in front of it somebody else's problem;
* an ``outage`` that started when the test *asked* for the close charged the harness's thread hop to the product — a
  103.2 ms reading on a CI runner (defect 60);
* the clock that starts when `close()` **returns** fixes that but is optimistic in the other direction: the listening
  socket stops accepting *inside* `close()`, so a request is already unserved while the coroutine is still running
  (`wait_closed()`, the pool drain, a loop thread descheduled before the caller is woken). That interval is downtime
  and it must be counted.

So this module measures the thing from outside our threads: real requests, in a loop, on the port the child was told.

    any HTTP status line   -> served (including the proxy's own 504 for an unreachable upstream)
    connect refused        -> not served (the listener is gone)
    connect, no answer     -> not served

Two things were tried and rejected because they were measured to be wrong, and both are worth knowing before changing
this file:

**A connect-only probe.** It cannot hang, which is tempting, and it is a lie exactly when it matters: a listening socket
that has been closed keeps completing handshakes from its accept queue for as long as the queue has room, so connections
are *accepted* for tens of milliseconds after the listener is gone — 46 ms of a 60 ms stall, measured — while nothing
answers them. "Connected" is not "serving".

**A tight answer timeout (10 ms).** It keeps a hung request from blinding the probe, and on a Windows runner every
attempt came back `timeout@15.5ms` — a loopback request through this proxy answers in ~16 ms there (timer granularity),
so the probe reported that a serving listener was not serving. A cap is only honest above the floor of the machine, and
that floor is not knowable from here.

What is done instead: the answer cap is **measured on this machine at start-up** (`calibrate`), and several requests are
in flight at once, so a request that is never answered cannot blind the probe for longer than one attempt.

Every number from here is bounded by the probe's own cycle. `cycle_ms()` reports it and callers print it beside the
numbers, because a measurement whose resolution is not stated is a number pretending to be a fact.
"""
from __future__ import annotations

import http.server
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

REQUEST_PATH = "/openai/v1/models"


@dataclass(frozen=True)
class Attempt:
    """One request, from the caller's point of view. `served` means the listener answered, not that an upstream did."""

    started: float                    # perf_counter, taken immediately before the connect
    finished: float
    connected: bool
    served: bool
    detail: str = ""                  # "HTTP 504", "refused", "timeout", "no-answer", "ConnectionResetError"

    @property
    def ms(self) -> float:
        return (self.finished - self.started) * 1000.0


def try_serve(port: int, *, path: str = REQUEST_PATH, timeout: float = 0.05,
              host: str = "127.0.0.1") -> Attempt:
    """One request through the listener. Never raises: a failure is the measurement.

    `timeout` bounds the connect and the wait for an answer; `ServingProbe` lowers it *after* measuring how fast this
    machine answers, never before (see the module docstring).
    """
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: blackbox-probe\r\nConnection: close\r\n\r\n".encode())
            head = sock.recv(64)
            finished = time.perf_counter()
            if head.startswith(b"HTTP/"):
                status = head.split(b" ", 2)[1].decode("ascii", "replace") if b" " in head else "?"
                return Attempt(started, finished, True, True, f"HTTP {status}")
            return Attempt(started, finished, True, False, "no-answer")
    except ConnectionRefusedError:
        return Attempt(started, time.perf_counter(), False, False, "refused")
    except TimeoutError:                        # includes socket.timeout: the connect or the answer expired
        return Attempt(started, time.perf_counter(), True, False, "timeout")
    except OSError as exc:                      # reset, unreachable, and anything else the kernel says
        return Attempt(started, time.perf_counter(), False, False, type(exc).__name__)


class LocalUpstream:
    """A tiny local HTTP server that answers 200 to anything: the probe's upstream, so a probe request is *served*.

    Pointing the proxy's route at a closed port looks like a shortcut and is not one: the proxy answers such a request
    with its own 504, and on a Windows runner that path takes longer than the probe's attempt budget (the SYN to a dead
    port is retried rather than refused there), so every probe attempt reads as a timeout and the measurement turns
    into a measurement of the failure path. A live upstream answers in the machine's own floor (~16 ms on Windows,
    ~0.5 ms on Linux loopback), which is exactly what `calibrate` needs to see.
    """

    def __init__(self) -> None:
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="blackbox-probe-upstream", daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> LocalUpstream:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(5)

    def __enter__(self) -> LocalUpstream:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    class _handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.do_GET()

        def log_message(self, *args: object) -> None:
            """Silent: the probe issues thousands of requests."""


class ServingProbe:
    """Threads issuing `try_serve` back to back, so the interval a child could not be served becomes observable.

    Several threads, because at the instant a listener goes away every request in flight is unanswered: a single-threaded
    probe is blind for one whole attempt timeout exactly when the measurement starts. `interval_s` is a rest between
    attempts (0 = as fast as the loopback allows). Resolution is the whole cycle, which `cycle_ms()` reports.
    """

    MIN_ANSWER_TIMEOUT_S = 0.010      # below this a loopback answer is indistinguishable from a hang
    CALIBRATION_SAMPLE = 5

    def __init__(self, port: int, path: str = REQUEST_PATH, *, interval_s: float = 0.0,
                 timeout: float = 0.05, threads: int = 3) -> None:
        self.port = port
        self.path = path
        self.interval_s = interval_s
        self.timeout = timeout
        self.threads = max(1, threads)
        self.answer_timeout_s = timeout       # until `calibrate` has measured this machine
        self.attempts: list[Attempt] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def start(self) -> ServingProbe:
        for index in range(self.threads):
            worker = threading.Thread(target=self._run, name=f"blackbox-serving-probe-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)
        return self

    def calibrate(self, *, timeout: float = 2.0) -> float:
        """Wait for a few answered requests, then set the answer cap from what this machine actually does.

        Returns the cap in milliseconds. Called by `measure_outage` (and by the bench) before the break, because a
        fixed cap is a guess about a machine: 10 ms is generous on Linux loopback (0.5 ms) and impossible on Windows
        (~16 ms), where it made every attempt look like a timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                served = [attempt for attempt in self.attempts if attempt.served]
            if len(served) >= self.CALIBRATION_SAMPLE:
                break
            if not self.alive():
                raise RuntimeError(f"the serving probe stopped: {self._error!r}")
            time.sleep(0.0005)
        measured = self.cycle_ms(self.CALIBRATION_SAMPLE)
        if measured > 0.0:                      # never *lower* the cap on a machine that has not answered yet
            self.answer_timeout_s = min(self.timeout, max(self.MIN_ANSWER_TIMEOUT_S, 3 * measured / 1000.0))
        return self.answer_timeout_s * 1000.0

    def stop(self) -> None:
        self._stop.set()
        for worker in self._workers:
            worker.join(5)
        if self._error is not None:
            raise RuntimeError(f"the serving probe failed: {self._error!r}") from self._error

    def __enter__(self) -> ServingProbe:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                attempt = try_serve(self.port, path=self.path, timeout=self.answer_timeout_s)
            except BaseException as exc:            # never leaves a worker dead without saying so
                self._error = exc
                return
            with self._lock:
                self.attempts.append(attempt)
                if len(self.attempts) > 100_000:          # bounded memory for a long-running probe
                    del self.attempts[:50_000]
            if self.interval_s:
                time.sleep(self.interval_s)

    # -- questions ---------------------------------------------------------------------------------------------
    def mark(self) -> int:
        """An index into `attempts`; everything at or after it happened after this call."""
        with self._lock:
            return len(self.attempts)

    def since(self, mark: int, *, timeout: float = 5.0) -> list[Attempt]:
        """Waits for at least one attempt at or after `mark`; raises rather than hangs if the probe is not running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.attempts) > mark:
                    return list(self.attempts[mark:])
            if not self.alive():
                raise RuntimeError(f"the serving probe stopped: {self._error!r}")
            time.sleep(0.0002)
        raise RuntimeError(f"the serving probe recorded nothing in {timeout} s")

    def recent(self, mark: int) -> list[Attempt]:
        """Everything at or after `mark`, possibly empty. Never waits — a caller with a deadline wants this one."""
        with self._lock:
            return list(self.attempts[mark:])

    def alive(self) -> bool:
        return bool(self._workers) and any(worker.is_alive() for worker in self._workers)

    def cycle_ms(self, sample: int = 200) -> float:
        """Median wall time of one **answered** attempt: the probe's resolution while there is service to measure.

        Answers only. A refused connect returns in microseconds, so a median taken after an outage would report
        ~0.01 ms and make the resolution of the measurement look far better than it is exactly when it matters.
        """
        with self._lock:
            served = [attempt for attempt in self.attempts[-20 * sample:] if attempt.served][-sample:]
        durations = sorted(attempt.ms for attempt in served)
        return durations[len(durations) // 2] if durations else 0.0


def measure_outage(probe: ServingProbe, break_listener: Callable[[], object], *,
                   timeout: float = 5.0) -> dict[str, object]:
    """Run `break_listener()` while the probe requests service, and return the outage a child would have seen.

    The clock starts at the **first request that was not served** — when the socket actually stopped answering,
    whenever that was relative to `close()` returning — and stops at the first request served afterwards. Two numbers
    come back:

    * `outage_ms` — that interval, the tightest estimate the probe can make;
    * `outage_upper_ms` — from the last request served **before** the break to the first served after it. This is the
      conservative bound and the one a budget should be asserted on: if no request happened to be refused (the outage
      was shorter than one cycle), the tight estimate has no start and this one still covers the gap.

    `started_break` is taken **before** `break_listener()` is called: a break that blocks until `close()` returns puts
    the whole downtime before the call's return, and a search that starts at the return finds nothing to report.
    """
    probe.calibrate()                    # measures this machine's answer floor into `answer_timeout_s`
    mark = probe.mark()
    attempts_before = _wait_for(probe, mark, lambda a: a.served, "was served", min(timeout, 2.0))
    last_served_before = max((a for a in attempts_before if a.served), key=lambda a: a.started)

    started_break = time.perf_counter()
    break_listener()
    settle = max(2 * probe.cycle_ms() / 1000.0, 0.001)

    unserved: Attempt | None = None
    served_again: Attempt | None = None
    cursor = mark
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = sorted(probe.recent(cursor), key=lambda a: a.started)
        cursor += len(fresh)
        for attempt in fresh:
            # Only attempts that were still outstanding at the break, or began after it, can describe the outage:
            # one that finished before the break is history.
            if attempt.finished < started_break or attempt.started <= last_served_before.started:
                continue
            if unserved is None:
                if not attempt.served:
                    unserved = attempt
            elif served_again is None and attempt.served and attempt.started > unserved.started:
                served_again = attempt
        if served_again is not None:
            break
        if unserved is None and time.monotonic() > started_break + settle:
            # Nothing was unserved inside the settle window: the outage was shorter than the probe can see, and the
            # first answer after the window is where the measurement ends rather than where service returned.
            served_again = next((a for a in sorted(probe.recent(mark), key=lambda a: a.started)
                                 if a.served and a.started >= started_break + settle), None)
            if served_again is not None:
                break
        if not probe.alive():
            raise RuntimeError(f"the serving probe stopped: {probe._error!r}")
        time.sleep(0.0002)
    if served_again is None:
        raise RuntimeError(f"the listener did not come back within {timeout} s of the break")

    start = max(unserved.started, started_break) if unserved is not None else last_served_before.started
    window = probe.recent(mark)
    first_failure = unserved.started if unserved is not None else start
    failed = [a for a in window if not a.served and first_failure <= a.started <= served_again.started]
    return {
        "outage_ms": max(0.0, (served_again.started - start) * 1000.0),
        "outage_upper_ms": max(0.0, (served_again.started - last_served_before.started) * 1000.0),
        "observed_failure": unserved is not None,
        "refused_requests": len(failed),
        "refused_details": sorted({a.detail for a in failed}),
        "served_again": served_again.started,
        "last_served_before": last_served_before.started,
        "probe_cycle_ms": probe.cycle_ms(),
        "probe_answer_timeout_ms": probe.answer_timeout_s * 1000.0,
    }


def _wait_for(probe: ServingProbe, mark: int, keep: Callable[[Attempt], bool], what: str,
              timeout: float) -> list[Attempt]:
    """Attempts at or after `mark` (sorted by start) once one of them satisfies `keep`; raises on the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = sorted(probe.recent(mark), key=lambda a: a.started)
        if any(keep(a) for a in attempts):
            return attempts
        if not probe.alive():
            raise RuntimeError(f"the serving probe stopped: {probe._error!r}")
        time.sleep(0.0005)
    seen = sorted(probe.recent(mark), key=lambda a: a.started)[-8:]
    details = ", ".join(f"{a.detail}@{a.ms:.2f}ms" for a in seen) or "no attempts at all"
    raise RuntimeError(f"no request {what} in {timeout:.1f} s: the probe is not measuring a serving listener "
                       f"(last attempts: {details})")


__all__ = ["Attempt", "LocalUpstream", "ServingProbe", "measure_outage", "try_serve"]
