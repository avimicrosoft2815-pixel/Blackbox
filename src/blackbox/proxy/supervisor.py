"""P3B-03's first half: the proxy as a thread the launcher can own, and the rules for the variables it exports.

A CLI process cannot hand its event loop to a proxy and then also wait on a child, so the proxy runs in its own thread
with its own loop. Two things follow from that, and both are the reason this file exists rather than four lines inside
`launch()`:

* **The thread must be supervised, not merely started.** A listener that dies (a port taken by another process, a
  socket error nobody was watching) would leave a child pointed at a base URL that answers nothing, which is worse
  than no recorder at all: the application fails and it fails *because of us*. So the thread watches its listener and
  rebinds, and the rebind's latency is a number the tests assert rather than a hope.
* **The variable plan is a pure function.** `OPENAI_BASE_URL` and friends may already be set — by the user, by a
  router, by a previous tool — and a recorder that overwrites them changes where the application's traffic goes. The
  rule is: export only what is unset, and **adopt** what is set as an upstream, because that value names a destination
  the user already chose and the proxy has to be allowed to reach it (P3B-02's allowlist is the route table, so a
  variable left alone would otherwise be a route to nowhere).

`plan_exports()` returns the exports and the routes together, from the same pass over the same variables, so the two
cannot drift.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

from blackbox.cap_sdk import headers as cap_headers
from blackbox.proxy.limits import raise_fd_limit
from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer

# The variables the plan names (§13): OpenAI, Anthropic, Ollama and Gemini base URLs.
BASE_URL_VARS: tuple[tuple[str, str], ...] = (
    ("OPENAI_BASE_URL", "openai"),
    ("ANTHROPIC_BASE_URL", "anthropic"),
    ("OLLAMA_HOST", "ollama"),
    ("GOOGLE_GEMINI_BASE_URL", "gemini"),
)
# The version segment each client expects *inside* the value it is given, because the client appends only the rest of
# its own path after it. The OpenAI SDK's own default is `https://api.openai.com/v1` and it appends `/chat/completions`;
# the Anthropic SDK's default is `https://api.anthropic.com` and it appends `/v1/messages`. Exporting one shape for both
# sent the OpenAI SDK to `/chat/completions` on the upstream, a path the provider does not serve (defect 39, P3B-09).
BASE_URL_SUFFIX: dict[str, str] = {"OPENAI_BASE_URL": "/v1"}
# One spelling of the variable: the SDK's header module decides what counts as "a proxy is configured", and the launcher
# must set exactly that variable to exactly that value or the `exact` link never happens (P3B-04).
ENV_PROXY = cap_headers.ENV_PROXY
# §11.8: "if it dies, the listener is re-bound within 100 ms". The subject of that sentence is the *listener coming
# back*, which is detection (a watchdog can only notice on a tick) plus the rebind. Measured with `bench/bench_rebind.py`
# on 2026-11-16: the rebind itself is p50 0.76 ms, and the budget was therefore being spent almost entirely on the tick.
# A 50 ms tick put the worst-case outage at 51.9 ms — inside the budget, but with only 2x left for whatever a shared
# runner does to a woken thread, and a 107.9 ms sample is what that looks like when it happens (defect 40).
#
# So the tick is 20 ms: worst-case outage ~21 ms, 4.7x under the budget, and detecting one attribute on a listener 50
# times a second is not a cost anybody can measure. This is a design change, not a statistic: ADR-006 records the
# definition and the distribution, and `tests/test_proxy_supervisor.py` asserts *every* watchdog sample, not the median.
REBIND_BUDGET_MS = 100.0
SUPERVISE_MS = 20.0                    # the default tick; a caller may pass any value it can defend
START_TIMEOUT = 10.0


@dataclass
class ExportPlan:
    """What the child sees, and what the proxy is thereby allowed to reach."""

    exports: dict[str, str] = field(default_factory=dict)        # filled by `with_url()`, once the port exists
    point: dict[str, str] = field(default_factory=dict)          # variable → the route it should be pointed at
    adopted: dict[str, str] = field(default_factory=dict)        # variable → the upstream it already named
    # `--force-proxy` took a value away from the child and pointed it at the listener instead. Kept separately from
    # `adopted` because it is the one case where the recorder *did* move traffic, so the surface has to say so by name.
    overridden: dict[str, str] = field(default_factory=dict)     # variable → the value it had
    routes: list[Route] = field(default_factory=list)
    default_route: str | None = None

    def with_url(self, proxy_url: str) -> ExportPlan:
        """Render the exports now that the listener has a port — the second half of a plan made before it did."""
        root = proxy_url.rstrip("/")
        rendered = {var: f"{root}/{name}{BASE_URL_SUFFIX.get(var, '')}" for var, name in self.point.items()}
        rendered[ENV_PROXY] = "1"
        return replace(self, exports=rendered)

    def as_dict(self) -> dict[str, Any]:
        return {"exports": dict(sorted(self.exports.items())), "point": dict(sorted(self.point.items())),
                "adopted": dict(sorted(self.adopted.items())), "overridden": dict(sorted(self.overridden.items())),
                "upstreams": [route.name for route in self.routes], "default_route": self.default_route}


def plan_exports(env: dict[str, str], proxy_url: str | None = None, *, force: bool = False) -> ExportPlan:
    """Union the child's variables with the proxy's routes, without ever overwriting the child's own value.

    Called with no `proxy_url` it decides *what to point where* (`plan.point`), which is what the launcher needs
    before it can start a listener; `plan.with_url(url)` fills in the values afterwards. Called with a URL it does
    both, which is what the tests and any caller that already has a port want.

    `force=True` is `--force-proxy`, and it means exactly one thing: **this run goes through the listener**, including
    a variable the user had already pointed somewhere. It is not a quiet default — the value that was displaced is
    returned in `plan.overridden` so the CLI can name it on stderr and the capsule can record it, because a recorder
    that moves traffic without saying which variable it moved is the failure mode the whole design avoids.
    """
    plan = ExportPlan()
    if force:
        for var, name in BASE_URL_VARS:
            value = (env.get(var) or "").strip()
            if value:
                plan.overridden[var] = value
            plan.point[var] = name
        pointed = [name for var, name in BASE_URL_VARS if var in plan.point]
        plan.default_route = "openai" if "openai" in pointed else (pointed[0] if pointed else None)
        return plan.with_url(proxy_url) if proxy_url is not None else replace(plan, exports={})
    adopted: list[Route] = []
    for var, name in BASE_URL_VARS:
        value = (env.get(var) or "").strip()
        if value:
            plan.adopted[var] = value
            if _host_of(value) is not None:
                adopted.append(Route(name=f"{name}-own", base=value, prefix=f"/{name}"))
        else:
            plan.point[var] = name
    plan.routes = adopted
    # The route a request lands on when nothing else claims it: the first variable that was ours to point, or the
    # first one that was already pointing somewhere. Never invented — if every variable was set, there is no default
    # and `Routes` will refuse to build one (`BBX-E140`), which is the honest outcome.
    pointed = [name for var, name in BASE_URL_VARS if var in plan.point]
    plan.default_route = "openai" if "openai" in pointed else (pointed[0] if pointed else
                                                               (adopted[0].name if adopted else None))
    return plan.with_url(proxy_url) if proxy_url is not None else replace(plan, exports={})


def _host_of(url: str) -> str | None:
    """`scheme://host[:port][/path]` → a routable base, or `None` when the value is not a URL this proxy can use."""
    if "://" not in url:
        return None
    scheme, _, rest = url.partition("://")
    if scheme not in ("http", "https") or not rest.split("/")[0]:
        return None
    return url


class ProxyThread:
    """A `ProxyServer` on its own loop in its own thread, with a watchdog that rebinds a dead listener."""

    def __init__(self, routes: Routes, *, receiver: Any = None, port: int = 0,
                 supervise_ms: float = SUPERVISE_MS, retry_transmitted: bool = False) -> None:
        self.routes = routes
        self.receiver = receiver
        self.retry_transmitted = retry_transmitted
        self._wanted_port = port
        # The port a listener actually got. Kept here rather than read off `self._server`, because a rebind has to know
        # which port to come back on *after* the old listener is closed and `_server` is `None` — reading it from the
        # server is how a loop-side restore took a fresh port on its first attempt.
        self._port = port
        self._supervise_ms = supervise_ms
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._server: ProxyServer | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        # Early notice from a listener that is being closed (defect 58): set by `ProxyServer.lost_cb`, waited on by the
        # watchdog, so a close is noticed in microseconds instead of at the next tick. The tick stays as the backstop
        # for a listener that dies without saying so — an OS error, a thread killed from outside, or a close that
        # happened before the callback was attached.
        self._wake = threading.Event()
        self.lost_at: float | None = None                  # monotonic time of the last early notice
        self.noticed_ms: list[float] = []                  # per detection: notify -> the watchdog saw it

        self._start_error: BaseException | None = None
        self.rebinds: list[float] = []                     # milliseconds each rebind took
        # How late each watchdog tick woke, in milliseconds: `interval - tick`. A rebind cannot start before the
        # watchdog notices, so a starved thread (a loaded CI runner, a stopped-the-world GC pause) spends the budget
        # before a line of our code runs. Recording it is how a red row distinguishes "our rebind was slow" from "the
        # thread did not get the CPU" instead of guessing (defect 58).
        self.wake_late_ms: list[float] = []
        # A restore the loop started for itself, when the listener said it was closed (defect 58b). The early notice
        # fires *on the loop thread*, which is already running; handing the work to the watchdog thread and then waiting
        # for the loop to schedule a coroutine costs one cross-thread round trip, and on a CI runner that round trip was
        # measured at 103.04 ms — the whole budget, spent waiting for a thread the OS had descheduled. So the loop
        # repairs its own listener, and the tick stays for the listeners that die without saying so.
        self._restoring = False
        # Set *synchronously* by the notice, before the loop has had a chance to start the task. `_restoring` alone is
        # not enough: it is set inside the coroutine, so between the close returning and the task being scheduled the
        # watchdog tick can look at a listener that is gone, call `rebind()`, and race the loop for the port. The loser
        # of that race took a new port — which is the one outcome a rebind must never produce.
        self._restore_pending = False
        self.restores_from_loop = 0
        self.port_changed = False                          # set when a rebind could not keep the port the child was told
        self.fd_limit: tuple[int, int] | None = None        # (before, after) descriptors, None where the OS has none
        self.pending_at_stop = 0                           # tasks still pending when the loop was drained (defect 61)
        self.undrained_at_stop = 0                         # ...and how many survived the drain (must be 0)
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------------------------------------------
    def start(self, timeout: float = START_TIMEOUT) -> ProxyThread:
        self._thread = threading.Thread(target=self._run, name="blackbox-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"the proxy thread did not start: {self._start_error!r}")
        if self._start_error is not None:
            raise RuntimeError(f"the proxy could not start: {self._start_error!r}") from self._start_error
        if self._supervise_ms > 0:
            self._watchdog = threading.Thread(target=self._supervise, name="blackbox-proxy-watchdog", daemon=True)
            self._watchdog.start()
        return self

    def _run(self) -> None:
        try:
            # Two descriptors per in-flight request, so the descriptor table *is* the concurrency limit (defect 62).
            # Raising the soft limit to what the OS already granted is not a policy: refusing a request the upstream
            # would have served is, and that one is not decided here.
            self.fd_limit = raise_fd_limit()
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                ProxyServer(self.routes, receiver=self.receiver, port=self._wanted_port,
                            retry_transmitted=self.retry_transmitted).start())
            self._port = self._server.port
            self._server.lost_cb = self._listener_lost
        except BaseException as exc:                        # reported through `start()`, not swallowed
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        if self._server is not None:
            self._server.lost_cb = None
        self._stopping.set()
        self._wake.set()
        loop = self._loop
        if loop is not None and self._server is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._server.close(), loop).result(5)
        if loop is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        if self._watchdog is not None:
            self._watchdog.join(5)
        self._drain_and_close(self._loop)

    def _drain_and_close(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cancel what is still pending and close the loop (defect 61).

        A handler that was still waiting on an upstream when the listener closed is a *pending task*, and `loop.stop()`
        leaves it pending. The loop is then never closed, so the task is destroyed whenever the collector gets to it,
        which prints `Task was destroyed but it is pending!` and hands that socket to the GC instead of closing it.
        Windows showed one per shutdown (a request to a dead upstream there waits for the SYN retry); the warning is
        also exactly the shape of message that hides a real one later.

        Done after the loop thread has exited and before the loop is closed, on this thread: the loop is not running, so
        it can be driven to completion here for the cancellations only.
        """
        if loop is None or loop.is_closed():
            return
        # In rounds, because cancelling the pending tasks *runs the loop*, and running the loop runs the `connection
        # made` callbacks that were already queued — those create new handler tasks, which the first round cannot have
        # seen. Measured: one round left three handlers behind per shutdown under a probing client.
        with contextlib.suppress(Exception):
            for _ in range(8):
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                if not pending:
                    break
                self.pending_at_stop += len(pending)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        with contextlib.suppress(Exception):
            # Settle: an accept that was already queued when the listener closed still runs its `connection made`
            # callback the next time the loop turns, and *that* creates a handler task the rounds above cannot have
            # seen. Three short turns catch them; measured on this repository, without them a probing client left one
            # handler per shutdown to be destroyed by the collector.
            for _ in range(3):
                loop.run_until_complete(asyncio.sleep(0.002))
                late = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in late:
                    task.cancel()
                if late:
                    self.pending_at_stop += len(late)
                    loop.run_until_complete(asyncio.gather(*late, return_exceptions=True))
        with contextlib.suppress(Exception):
            # Last look: a task that is still pending here will be destroyed by the collector and announced. Recorded
            # rather than swallowed, so a test can assert the drain converged instead of trusting it.
            self.undrained_at_stop = sum(1 for task in asyncio.all_tasks(loop) if not task.done())
        with contextlib.suppress(Exception):
            loop.close()

    # -- introspection -------------------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.port if self._server is not None else self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self._server is None else self._server.url

    @property
    def server(self) -> ProxyServer | None:
        return self._server

    @property
    def stats(self) -> Any:
        return self._server.stats if self._server is not None else None

    # -- supervision ---------------------------------------------------------------------------------------------
    def _listener_lost(self) -> None:
        """Called on the loop thread the instant a listener is closed, and nothing else.

        It wakes the watchdog (the backstop stays wired) **and** schedules the repair on the loop, because the loop is
        the thread that is already running and the one that will have to serve the child anyway. Doing the rebind here
        removes a hop that a loaded machine charges in full: `run_coroutine_threadsafe(...).result()` waits for a thread
        to be scheduled, and on a GitHub runner that wait was 103 ms of a 100 ms budget (defect 60).
        """
        self.lost_at = time.monotonic()
        with self._lock:
            self._restore_pending = True
        self._wake.set()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if threading.current_thread() is self._thread:
                loop.call_soon(self._restore_from_loop)
            else:
                loop.call_soon_threadsafe(self._restore_from_loop)
        except RuntimeError:                       # the loop is shutting down; the watchdog has already been woken
            pass

    def _restore_from_loop(self) -> None:
        """Runs in the loop thread: turn the notice into a task, so the close finishes before the start begins."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._stopping.is_set():
            return
        loop.create_task(self._restore_on_loop())

    async def _restore_on_loop(self) -> None:
        """Close what is left of the dead listener and bind its port again, all on the loop.

        One owner at a time: `_restoring` is set before the first `await`, and the watchdog skips a tick while it is
        set. Two restores racing would bind the port twice and the loser would fall back to a new port, which is the one
        thing a rebind must not do — the child was told the old one.
        """
        if self._restoring or self._stopping.is_set():
            return
        current = self._server
        if current is not None and current.is_serving:
            with self._lock:
                self._restore_pending = False
            return
        self._restoring = True
        started = time.monotonic()
        try:
            if current is not None:
                current.lost_cb = None
                with contextlib.suppress(Exception):
                    await current.close()
            self._server = await self._restart_on_loop()
            self._port = self._server.port
            self._server.lost_cb = self._listener_lost
            self.rebinds.append((time.monotonic() - started) * 1000.0)
            self.restores_from_loop += 1
        except Exception as exc:                   # the watchdog tick is the backstop; do not kill the loop
            self._start_error = exc
        finally:
            self._restoring = False
            with self._lock:
                self._restore_pending = False

    async def _restart_on_loop(self) -> ProxyServer:
        """Same port first, and the same fallback as `_restart`, but awaited instead of hop-and-wait.

        The old port is retried before it is given up on: the socket the listener just closed can still be inside the
        kernel for a moment under load, and a rebind that moves the port breaks every child that was told the old one.
        A full-suite run on a busy machine hit exactly that (the port assert caught it), so three short attempts come
        before the fallback that `port_changed` records.
        """
        for attempt in range(3):
            try:
                return await ProxyServer(self.routes, receiver=self.receiver, port=self.port,
                                         retry_transmitted=self.retry_transmitted).start()
            except Exception as exc:
                if attempt == 2:
                    last = exc
                    break
                await asyncio.sleep(0.002)
        self.port_changed = True
        for candidate in (0,):
            try:
                return await ProxyServer(self.routes, receiver=self.receiver, port=candidate,
                                         retry_transmitted=self.retry_transmitted).start()
            except Exception as exc:
                raise RuntimeError(f"the proxy could not rebind: {exc}") from last
        raise RuntimeError("unreachable")          # pragma: no cover - the loop above always returns or raises

    def _supervise(self) -> None:
        tick = self._supervise_ms / 1000.0
        previous = time.monotonic()
        while not self._stopping.is_set():
            # Woken either by the tick or by a listener saying it is gone. The flag is cleared *before* the check, so a
            # close that lands between the two sets it again and the next pass sees it.
            self._wake.wait(tick)
            self._wake.clear()
            now = time.monotonic()
            late = max(0.0, (now - previous - tick) * 1000.0)
            noticed = self.lost_at
            self.lost_at = None
            if noticed is not None and len(self.noticed_ms) < 4096:
                self.noticed_ms.append(max(0.0, (now - noticed) * 1000.0))
            if len(self.wake_late_ms) < 4096:
                self.wake_late_ms.append(late)
            previous = now
            if self._stopping.is_set():
                return
            if self._server is None or self._loop is None:
                continue
            if self._server.is_serving or self._restoring or self._restore_pending:
                continue
            self.rebind()

    def rebind(self, timeout: float = START_TIMEOUT) -> float:
        """Replace a dead listener and return how many milliseconds it took.

        Used by the watchdog's tick (which skips a pass while `_restoring`/`_restore_pending` says the loop is already
        repairing) and by callers that ask for one on purpose. The loop-side path is `_restore_on_loop`.
        """
        with self._lock:
            started = time.monotonic()
            loop = self._loop
            if loop is None:
                raise RuntimeError("the proxy thread is not running")
            old = self._server
            if old is not None:
                old.lost_cb = None                 # our own rebind is not a loss; the watchdog is already here
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(old.close(), loop).result(5)
            self._server = self._restart(loop, timeout)
            if self._server is not None:
                self._port = self._server.port
                self._server.lost_cb = self._listener_lost
            elapsed = (time.monotonic() - started) * 1000.0
            self.rebinds.append(elapsed)
            return elapsed

    def _restart(self, loop: asyncio.AbstractEventLoop, timeout: float) -> ProxyServer:
        """Rebind on the **same** port when it can be had: the child was told that base URL and cannot be told again."""
        in_use = self.port
        for candidate in (in_use, 0):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    ProxyServer(self.routes, receiver=self.receiver, port=candidate,
                                retry_transmitted=self.retry_transmitted).start(), loop)
                return future.result(timeout)
            except Exception as exc:                        # the old port went to somebody else: take a new one
                if candidate == 0:
                    raise RuntimeError(f"the proxy could not rebind: {exc}") from exc
                self.port_changed = True
        raise RuntimeError("unreachable")                   # pragma: no cover - the loop above always returns or raises

    def __enter__(self) -> ProxyThread:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["BASE_URL_SUFFIX", "BASE_URL_VARS", "ENV_PROXY", "REBIND_BUDGET_MS", "SUPERVISE_MS", "ExportPlan",
           "ProxyThread", "plan_exports"]
