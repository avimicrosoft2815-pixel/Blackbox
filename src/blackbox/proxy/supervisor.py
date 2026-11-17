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
        self.port_changed = False                          # set when a rebind could not keep the port the child was told
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
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                ProxyServer(self.routes, receiver=self.receiver, port=self._wanted_port,
                            retry_transmitted=self.retry_transmitted).start())
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

    # -- introspection -------------------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.port if self._server is not None else self._wanted_port

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
        """Called on the loop thread the instant a listener is closed, and nothing else."""
        self.lost_at = time.monotonic()
        self._wake.set()

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
            if self._server.is_serving:
                continue
            self.rebind()

    def rebind(self, timeout: float = START_TIMEOUT) -> float:
        """Replace a dead listener and return how many milliseconds it took. Supervision calls this."""
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
