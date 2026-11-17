"""The child's-eye clock for a rebind: when a request first failed, and when one was served again.

The question §11.8 asks is not "how long did `rebind()` take" and not "when did `close()` return". It is: **during
which interval could a request not be served**. Every other clock is a proxy for that one, and each of them has been
wrong here at least once:

* `rebind_ms` (the old row) measures our own work and calls the queueing in front of it somebody else's problem;
* an ``outage`` that started when the test *asked* for the close charged the harness's thread hop to the product — a
  103.2 ms reading on a CI runner (defect 60);
* the clock that starts when `close()` **returns** fixes that but is optimistic in the other direction: the listening
  socket stops accepting *inside* `close()`, so a request is already refused while the coroutine is still running
  (`wait_closed()`, the pool drain, the loop thread descheduled before the caller is woken). That interval is downtime
  and it must be counted.

So this module measures the thing from outside our threads: one **raw socket request** in a tight loop through the
proxy on the port the child was told.

    connect refused            -> not served (the listener is gone)
    connect, no HTTP answer in 10 ms -> not served (the listener exists but nothing is serving; a hung request is
                                 downtime, and its cost is capped so the probe's patience cannot become the number)
    any HTTP status line       -> served (including the proxy's own 504 for an unreachable upstream)

A raw socket rather than `http.client` for one measured reason: `http.client` can block for its whole timeout on a
connection that was accepted just before the listener went away, and a probe that stops sampling during the outage it
is measuring reports "no failure observed" for an outage it slept through. This one finishes every attempt inside
`timeout`, and the attempt's **start** is what the clock uses, so a slow attempt is still a slow interval.

Every number from here is bounded by the probe's own cycle (attempt + rest). `cycle_ms()` reports it and callers print
it beside the number, because a measurement whose resolution is not stated is a number pretending to be a fact.

The request timeout is 50 ms, half the 100 ms budget, and it is part of the contract: a request the listener accepted
but never answered is charged at most 50 ms, so an end-to-end outage can never be inflated past the budget by one hung
socket, and a child that waited 50 ms for an answer has already spent half the budget it was promised. A longer timeout
would make the probe's own patience the dominant term in the measurement.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

REQUEST = b"GET /openai/v1/models HTTP/1.1\r\nHost: blackbox-probe\r\nConnection: close\r\n\r\n"


@dataclass(frozen=True)
class Attempt:
    """One request, from the caller's point of view. `served` means the listener answered, not that an upstream did."""

    started: float                    # perf_counter, taken immediately before the connect
    finished: float
    served: bool
    detail: str = ""                  # "HTTP 504", "refused", "timeout", "reset"

    @property
    def ms(self) -> float:
        return (self.finished - self.started) * 1000.0


ANSWER_TIMEOUT_S = 0.010      # 10 ms: 20x the p50 loopback answer and a tenth of the budget. See the module docstring.


def try_serve(port: int, *, path: str = "/openai/v1/models", timeout: float = 0.05,
              host: str = "127.0.0.1") -> Attempt:
    """One request through the listener. Never raises: a failure is the measurement.

    Two phases, deliberately different lengths. The **connect** gets `timeout` (50 ms) because a refused connect is
    instant and a successful one only needs to reach the kernel. The **answer** gets `ANSWER_TIMEOUT_S` (10 ms): a
    connection that was accepted just before the listener went away is never answered — it was in the backlog, not in
    the server's writer set — and waiting the full 50 ms for it would put the probe's own patience into the outage
    number. The first version did exactly that and reported a p95 of 50 ms on a rebind that took 0.4 ms.
    """
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(min(timeout, ANSWER_TIMEOUT_S))
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: blackbox-probe\r\nConnection: close\r\n\r\n".encode())
            head = sock.recv(64)
            finished = time.perf_counter()
            if head.startswith(b"HTTP/"):
                status = head.split(b" ", 2)[1].decode("ascii", "replace") if b" " in head else "?"
                return Attempt(started, finished, True, f"HTTP {status}")
            return Attempt(started, finished, False, "no-answer")
    except ConnectionRefusedError:
        return Attempt(started, time.perf_counter(), False, "refused")
    except TimeoutError:
        return Attempt(started, time.perf_counter(), False, "timeout")
    except OSError as exc:                      # reset, unreachable, and anything else the kernel says
        return Attempt(started, time.perf_counter(), False, type(exc).__name__)


class ServingProbe:
    """A thread issuing `try_serve` back to back, so the interval a child could not be served becomes observable.

    `interval_s` is a rest between attempts (0 = as fast as one loopback request allows). Resolution is the whole
    cycle, which `cycle_ms()` reports.
    """

    def __init__(self, port: int, path: str = "/openai/v1/models", *, interval_s: float = 0.0,
                 timeout: float = 0.05) -> None:
        self.port = port
        self.path = path
        self.interval_s = interval_s
        self.timeout = timeout
        self.attempts: list[Attempt] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def start(self) -> ServingProbe:
        self._thread = threading.Thread(target=self._run, name="blackbox-serving-probe", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        if self._error is not None:
            raise RuntimeError(f"the serving probe failed: {self._error!r}") from self._error

    def __enter__(self) -> ServingProbe:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            attempt = try_serve(self.port, path=self.path, timeout=self.timeout)
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
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError(f"the serving probe stopped: {self._error!r}")
            time.sleep(0.0002)
        raise RuntimeError(f"the serving probe recorded nothing in {timeout} s")

    def recent(self, mark: int) -> list[Attempt]:
        """Everything at or after `mark`, possibly empty. Never waits — a caller with a deadline wants this one."""
        with self._lock:
            return list(self.attempts[mark:])

    def cycle_ms(self, sample: int = 200) -> float:
        """Median wall time of one **served** attempt: the probe's resolution while there is service to measure.

        Refusals are not the denominator. A refused connect returns in microseconds, so a median taken after an outage
        would report ~0.01 ms and make the resolution of the measurement look 20x better than it is exactly when the
        number matters (the first version did).
        """
        with self._lock:
            served = [attempt for attempt in self.attempts[-20 * sample:] if attempt.served][-sample:]
        durations = sorted(attempt.ms for attempt in served)
        return durations[len(durations) // 2] if durations else 0.0

    def failures(self, mark: int, *, after: float = 0.0) -> list[Attempt]:
        return [attempt for attempt in self.since(mark) if not attempt.served and attempt.started >= after]


def measure_outage(probe: ServingProbe, break_listener: Callable[[], object], *,
                   timeout: float = 5.0) -> dict[str, object]:
    """Run `break_listener()` while the probe requests service, and return the outage a child would have seen.

    The clock starts at the **first request that was not served** — which is when the socket actually stopped taking
    requests, whenever that was relative to `close()` returning — and stops at the first request served afterwards.
    Two numbers come back:

    * `outage_ms` — that interval, the tightest estimate the probe can make;
    * `outage_upper_ms` — from the last request served **before** the break to the first served after it. This is the
      conservative bound and the one a budget should be asserted on: if no request happened to be refused (the outage
      was shorter than one cycle), the tight estimate has no start and this one still covers the gap.
    """
    mark = probe.mark()
    started_waiting = time.monotonic()
    last_served_before: Attempt | None = None
    while True:
        attempts_before = probe.recent(mark)
        served_before = [a for a in attempts_before if a.served]
        if served_before:
            last_served_before = max(served_before, key=lambda a: a.started)
            break
        if time.monotonic() - started_waiting > min(timeout, 2.0):
            recent = ", ".join(f"{a.detail}@{a.ms:.2f}ms" for a in attempts_before[-8:]) or "no attempts at all"
            raise RuntimeError(
                f"no request was served in {min(timeout, 2.0):.1f} s: the probe is not measuring a serving listener "
                f"(last attempts: {recent})")
        if probe._thread is not None and not probe._thread.is_alive():
            raise RuntimeError(f"the serving probe stopped: {probe._error!r}")
        time.sleep(0.0005)
    # Taken *before* the call: a break that blocks until `close()` returns (which is what the product's callers do)
    # would otherwise put the whole downtime before the window this function searches.
    started_break = time.perf_counter()
    break_listener()
    failure = None
    served_after = None
    cursor = mark
    # Until a failure is seen, a served attempt could be one that was in flight when the break was asked for and
    # finished against a listener that had not stopped yet (a true reading, but not evidence that service survived the
    # break). So the no-failure branch waits `settle` past the break before it accepts a served attempt: at least three
    # probe cycles, so the sample is wider than the resolution rather than narrower than it.
    settle = max(3 * probe.cycle_ms() / 1000.0, 0.001)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = probe.recent(cursor)          # incremental: the probe can add thousands of attempts in a stall
        cursor += len(fresh)
        for attempt in fresh:
            # An attempt that was *outstanding* when the break happened counts: it can finish unserved because the
            # listener went away underneath it, and those are exactly the requests a child has in flight. Its start is
            # not allowed to move the clock before the break (nothing was asked of the listener before that), which is
            # what `max(failure.started, started_break)` below enforces.
            if attempt.finished < started_break or attempt.started <= last_served_before.started:
                continue
            if failure is None and not attempt.served:
                failure = attempt
                continue
            if failure is None and attempt.started < started_break + settle:
                continue
            if attempt.served and (failure is None or attempt.started > failure.started):
                served_after = attempt
                break
        if served_after is not None:
            break
        time.sleep(0.0002)
    if served_after is None:
        raise RuntimeError(f"no request was served within {timeout} s of the break: the listener did not come back")
    start = max(failure.started, started_break) if failure is not None else last_served_before.started
    refused = [a for a in probe.recent(mark)
               if not a.served and failure is not None and failure.started <= a.started <= served_after.started]
    return {
        "outage_ms": (served_after.started - start) * 1000.0,
        "outage_upper_ms": (served_after.started - last_served_before.started) * 1000.0,
        "first_failure": None if failure is None else {"started": failure.started, "detail": failure.detail},
        "refused_requests": len(refused),
        "refused_details": sorted({a.detail for a in refused}),
        "observed_failure": failure is not None,
        "probe_cycle_ms": probe.cycle_ms(),
        "served_again": served_after.started,
        "last_served_before": last_served_before.started,
    }


__all__ = ["Attempt", "ServingProbe", "measure_outage", "try_serve"]
