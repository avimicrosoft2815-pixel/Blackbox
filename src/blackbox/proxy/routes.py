"""P3B-02: which upstream a proxied request goes to — and which upstreams exist at all.

The proxy is the universality promise: `blackbox run -- node app.js` works because every SDK honours a base-URL
environment variable, so pointing that variable at loopback is the whole trick. Which means the interesting decisions
are not about bytes but about *destinations*:

* **An upstream is a name and a URL, and the URL is the allowlist.** A request can only ever leave for a host this
  process was configured with. There is no path from a header, a path or a redirect to a host that is not in the
  table, because the table is the only thing that can produce a destination — `Routes.resolve()` returns a `Route`,
  never a host string, and `proxy/server.py` has no other source for `open_connection`.
* **Routing has a fixed order**, so "which upstream did that go to?" is answerable without reading the server:
  the `x-blackbox-upstream` header names a route explicitly (and an unknown name is an error, never a fallback);
  then the longest path prefix wins (`/openai/v1/...` strips `/openai`); then the single default route, if there is
  one; otherwise the request is `502 upstream-not-allowed` (`BBX-E140`).
* **The bind is loopback, and the exception has a name.** A proxy that answers the LAN is a recorder that records
  other people's traffic, so a non-loopback bind raises `BBX-E143` unless the caller says `bind_unsafe=True`, which
  is a sentence in a CLI flag rather than a default anyone arrives at by accident.

Prefix stripping is deliberately part of the route rather than a rewrite engine: `strip=True` removes exactly the
matched prefix, so `http://127.0.0.1:PORT/openai/v1/models` with route `openai` at `/openai` becomes
`api.openai.com/v1/models` and nothing else changes — no trailing-slash guessing, no query rewriting.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from blackbox.errors import BlackBoxError, bbx

UPSTREAM_HEADER = "x-blackbox-upstream"          # the route, by name; stripped before it reaches the upstream
PROXY_HEADERS_PREFIX = "x-blackbox-"             # every one of ours: never forwarded, in either direction
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = {"http": 80, "https": 443}
MAX_BODY_BYTES = 32 * 1024 * 1024


class RouteError(BlackBoxError):
    """A routing failure that the server turns into a `502`/`504` with `x-blackbox-error` on it."""

    def __init__(self, code: str, detail: str = "", *, reason: str = "upstream-not-allowed",
                 status: int = 502) -> None:
        super().__init__(code, detail)
        self.reason = reason
        self.status = status


def upstream_not_allowed(detail: str) -> RouteError:
    return RouteError("BBX-E140", detail, reason="upstream-not-allowed", status=502)


@dataclass(frozen=True)
class Route:
    """One named upstream. `base` may carry a path (`http://127.0.0.1:8000/v1`), a prefix, not a rewrite."""

    name: str
    base: str
    prefix: str = ""                 # inbound path prefix that belongs to this route
    strip: bool = True               # remove `prefix` before forwarding (the plan's "prefix strip")
    verify_tls: bool = True          # `--insecure-upstream` sets this False, for this route only
    allow_redirects: bool = True

    def __post_init__(self) -> None:
        parts = urlsplit(self.base)
        if parts.scheme not in ("http", "https"):
            raise upstream_not_allowed(f"route {self.name!r}: base {self.base!r} is not http(s)")
        if not parts.hostname:
            raise upstream_not_allowed(f"route {self.name!r}: base {self.base!r} has no host")
        if parts.query or parts.fragment:
            raise upstream_not_allowed(f"route {self.name!r}: base {self.base!r} must not carry a query")
        if self.prefix and not self.prefix.startswith("/"):
            raise upstream_not_allowed(f"route {self.name!r}: prefix {self.prefix!r} must start with '/'")
        object.__setattr__(self, "prefix", self.prefix.rstrip("/"))

    # -- destination ---------------------------------------------------------------------------------------------
    @property
    def scheme(self) -> str:
        return urlsplit(self.base).scheme

    @property
    def host(self) -> str:
        parsed = urlsplit(self.base)
        return parsed.hostname or ""

    @property
    def port(self) -> int:
        parsed = urlsplit(self.base)
        return parsed.port or DEFAULT_PORT[parsed.scheme]

    @property
    def base_path(self) -> str:
        return urlsplit(self.base).path.rstrip("/")

    @property
    def destination(self) -> tuple[str, int]:
        """The `(host, port)` this route can reach — the only thing `open_connection` is ever given."""
        return (self.host, self.port)

    def target_path(self, inbound_path: str) -> str:
        """Inbound path → the path the upstream sees: prefix stripped, the base's own path prepended."""
        path = inbound_path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if self.prefix and path == self.prefix:
            return self.base_path or "/"          # the bare prefix is the base URL, with no extra slash to explain
        if self.prefix and path.startswith(self.prefix + "/"):
            path = path[len(self.prefix):]
        return (self.base_path + path) or "/"

    def skip_verification(self) -> Route:
        """A copy of this route that does not verify upstream certificates — the `--insecure-upstream` flag's doing."""
        return replace(self, verify_tls=False)


class Routes:
    """The table. Immutable after construction except for `verify_all()`, which can only harden it."""

    def __init__(self, routes: list[Route] | tuple[Route, ...] = (), *, default: str | None = None,
                 bind: str = "127.0.0.1", bind_unsafe: bool = False) -> None:
        table: dict[str, Route] = {}
        for route in routes:
            if route.name in table:
                raise upstream_not_allowed(f"two upstreams are named {route.name!r}")
            table[route.name] = route
        if default is not None and default not in table:
            raise upstream_not_allowed(f"default route {default!r} is not in the table")
        self._routes = table
        self._default = default
        self._bind = check_bind(bind, bind_unsafe)
        self._by_prefix = sorted((r for r in table.values() if r.prefix), key=lambda r: len(r.prefix), reverse=True)

    # -- construction --------------------------------------------------------------------------------------------
    @classmethod
    def from_specs(cls, specs: list[str], *, default: str | None = None, bind: str = "127.0.0.1",
                   bind_unsafe: bool = False, unverified: tuple[str, ...] = ()) -> Routes:
        """`["openai=<provider base>", "local=http://127.0.0.1:8000/v1@/local"]` → a table.

        The optional `@/prefix` suffix is the inbound prefix; without it the route answers only by header name or as
        the default (`--upstream` in the CLI, P3B-03).
        """
        routes: list[Route] = []
        for spec in specs:
            name, sep, rest = spec.partition("=")
            if not sep or not name.strip():
                raise upstream_not_allowed(f"upstream {spec!r} is not name=url")
            url, _, prefix = rest.partition("@")
            routes.append(Route(name=name.strip(), base=url.strip(), prefix=prefix.strip(),
                                verify_tls=name.strip() not in unverified))
        return cls(routes, default=default, bind=bind, bind_unsafe=bind_unsafe)

    # -- introspection -------------------------------------------------------------------------------------------
    @property
    def bind(self) -> str:
        return self._bind

    @property
    def default(self) -> str | None:
        return self._default

    def names(self) -> list[str]:
        return sorted(self._routes)

    def route(self, name: str) -> Route:
        try:
            return self._routes[name]
        except KeyError:
            raise upstream_not_allowed(f"no upstream is named {name!r}") from None

    def destinations(self) -> set[tuple[str, int]]:
        """Exactly the `(host, port)` pairs the proxy can reach — the set `tests/test_no_egress.py` asserts."""
        return {route.destination for route in self._routes.values()}

    def as_dict(self) -> dict[str, object]:
        return {"bind": self._bind, "default": self._default,
                "upstreams": {name: {"base": r.base, "prefix": r.prefix, "strip": r.strip,
                                     "verify_tls": r.verify_tls} for name, r in sorted(self._routes.items())}}

    def verify_all(self) -> None:
        """Undo every `--insecure-upstream`: the only direction this table moves is towards verification."""
        self._routes = {name: replace(r, verify_tls=True) for name, r in self._routes.items()}

    # -- the decision --------------------------------------------------------------------------------------------
    def resolve(self, path: str, headers: object = ()) -> Route:
        """Path + headers → a route, in the documented order. Never returns a host that is not in the table."""
        named = _header(headers, UPSTREAM_HEADER)
        if named:
            route = self._routes.get(named.strip())
            if route is None:
                raise upstream_not_allowed(f"header {UPSTREAM_HEADER}: {named!r} names no configured upstream")
            return route
        clean = path.split("?", 1)[0] or "/"
        for route in self._by_prefix:
            if clean == route.prefix or clean.startswith(route.prefix + "/"):
                return route
        if self._default is not None:
            return self._routes[self._default]
        raise upstream_not_allowed(f"no route matches {clean!r} and there is no default upstream")


def check_bind(host: str, unsafe: bool = False) -> str:
    """Loopback, or an explicit `bind_unsafe`. A recorder that answers the LAN records other people's traffic."""
    if host in LOOPBACK_HOSTS:
        return host
    if not unsafe:
        raise RouteError("BBX-E143", f"refusing to bind {host!r}", reason="bind-refused", status=0)
    return host


def _header(headers: object, name: str) -> str | None:
    """Works for a list of `(name, value)` pairs (the server's type) and for a mapping (a test's or an SDK's)."""
    items = headers.items() if hasattr(headers, "items") else headers
    for key, value in items:                                  # type: ignore[union-attr]
        if isinstance(key, str) and key.lower() == name:
            return value if isinstance(value, str) else str(value)
    return None


def is_ours(name: str) -> bool:
    """Every `x-blackbox-*` header: ours, and never forwarded in either direction."""
    return name.lower().startswith(PROXY_HEADERS_PREFIX)


def coded_error(code: str, detail: str, *, reason: str, status: int = 502) -> RouteError:
    return RouteError(code, detail, reason=reason, status=status)


__all__ = [
    "DEFAULT_PORT", "LOOPBACK_HOSTS", "MAX_BODY_BYTES", "PROXY_HEADERS_PREFIX", "UPSTREAM_HEADER", "Route",
    "RouteError", "Routes", "bbx", "check_bind", "coded_error", "is_ours", "upstream_not_allowed",
]
