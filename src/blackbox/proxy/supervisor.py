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

from blackbox.proxy.routes import Route, Routes
from blackbox.proxy.server import ProxyServer

# The variables the plan names (§13): OpenAI, Anthropic, Ollama and Gemini base URLs.
BASE_URL_VARS: tuple[tuple[str, str], ...] = (
    ("OPENAI_BASE_URL", "openai"),
    ("ANTHROPIC_BASE_URL", "anthropic"),
    ("OLLAMA_HOST", "ollama"),
    ("GOOGLE_GEMINI_BASE_URL", "gemini"),
)
ENV_PROXY = "BLACKBOX_PROXY"
REBIND_BUDGET_MS = 100.0
START_TIMEOUT = 10.0


@dataclass
class ExportPlan:
    """What the child sees, and what the proxy is thereby allowed to reach."""

    exports: dict[str, str] = field(default_factory=dict)        # filled by `with_url()`, once the port exists
    point: dict[str, str] = field(default_factory=dict)          # variable → the route it should be pointed at
    adopted: dict[str, str] = field(default_factory=dict)        # variable → the upstream it already named
    routes: list[Route] = field(default_factory=list)
    default_route: str | None = None

    def with_url(self, proxy_url: str) -> ExportPlan:
        """Render the exports now that the listener has a port — the second half of a plan made before it did."""
        rendered = {var: f"{proxy_url.rstrip('/')}/{name}" for var, name in self.point.items()}
        rendered[ENV_PROXY] = "1"
        return replace(self, exports=rendered)

    def as_dict(self) -> dict[str, Any]:
        return {"exports": dict(sorted(self.exports.items())), "point": dict(sorted(self.point.items())),
                "adopted": dict(sorted(self.adopted.items())),
                "upstreams": [route.name for route in self.routes], "default_route": self.default_route}


def plan_exports(env: dict[str, str], proxy_url: str | None = None) -> ExportPlan:
    """Union the child's variables with the proxy's routes, without ever overwriting the child's own value.

    Called with no `proxy_url` it decides *what to point where* (`plan.point`), which is what the launcher needs
    before it can start a listener; `plan.with_url(url)` fills in the values afterwards. Called with a URL it does
    both, which is what the tests and any caller that already has a port want.
    """
    plan = ExportPlan()
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
                 supervise_ms: float = 50.0) -> None:
        self.routes = routes
        self.receiver = receiver
        self._wanted_port = port
        self._supervise_ms = supervise_ms
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._server: ProxyServer | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._start_error: BaseException | None = None
        self.rebinds: list[float] = []                     # milliseconds each rebind took
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
                ProxyServer(self.routes, receiver=self.receiver, port=self._wanted_port).start())
        except BaseException as exc:                        # reported through `start()`, not swallowed
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        self._stopping.set()
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
    def _supervise(self) -> None:
        while not self._stopping.wait(self._supervise_ms / 1000.0):
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
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(old.close(), loop).result(5)
            self._server = self._restart(loop, timeout)
            elapsed = (time.monotonic() - started) * 1000.0
            self.rebinds.append(elapsed)
            return elapsed

    def _restart(self, loop: asyncio.AbstractEventLoop, timeout: float) -> ProxyServer:
        """Rebind on the **same** port when it can be had: the child was told that base URL and cannot be told again."""
        in_use = self.port
        for candidate in (in_use, 0):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    ProxyServer(self.routes, receiver=self.receiver, port=candidate).start(), loop)
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


__all__ = ["BASE_URL_VARS", "ENV_PROXY", "REBIND_BUDGET_MS", "ExportPlan", "ProxyThread", "plan_exports"]
