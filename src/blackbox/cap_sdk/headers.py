"""`x-blackbox-run`: the one header that ties a proxy-written call to the run that made it — and its two rules.

The header is how §11.10's `exact` link is established across a process and a language boundary: the SDK knows which run
it is in, the proxy sees a socket that says nothing about runs, and one header closes that gap. It is also the only thing
in this product that touches *someone else's request*, so both rules are hard:

* **Inject only when a proxy is configured.** `BLACKBOX_PROXY=1` is set by `blackbox run` and by nothing else. An app that
  was never pointed at a loopback proxy must not have its requests modified at all — a recorder that adds a header to
  traffic going straight at OpenAI has changed the customer's bytes for its own bookkeeping, which is the one thing a
  flight recorder may never do. `inject()` is therefore a no-op unless that variable says `1`, and the test asserts against
  a mock provider that pretends to be the real one that nothing at all arrives when it does not.
* **Never let it reach the upstream.** The proxy strips every `x-blackbox-*` header in both directions before forwarding
  (`proxy/routes.py: is_ours`), so even a user who exported the variable by hand leaks an internal name to a provider at
  worst as far as the proxy, never further. The proxy *reads* the value first, because that value is the link.

The value is `<run_id>/<node>`; the node half is a hint (which call inside the run), and a reader that cannot parse it
keeps the run half rather than dropping the link.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

HEADER = "x-blackbox-run"
PREFIX = "x-blackbox-"
ENV_PROXY = "BLACKBOX_PROXY"
ENV_RUN = "BLACKBOX_RUN_ID"
ENABLED_VALUE = "1"


def proxy_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process was pointed at a loopback proxy by the launcher. Only `1` counts."""
    env = os.environ if environ is None else environ
    return env.get(ENV_PROXY, "").strip() == ENABLED_VALUE


def run_id(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = env.get(ENV_RUN, "").strip()
    return value or None


def value(environ: Mapping[str, str] | None = None, *, node: int | None = None) -> str | None:
    """The header's value, or `None` when this process must not send one."""
    if not proxy_configured(environ):
        return None
    run = run_id(environ)
    if run is None:
        return None
    return f"{run}/{node}" if node is not None else run


def inject(headers: Any, environ: Mapping[str, str] | None = None, *, node: int | None = None) -> bool:
    """Add the header to an outgoing request's headers. Returns whether it was added.

    `headers` is whatever the seam has: an `httpx.Headers`, a dict, anything that supports `__setitem__`. Nothing here
    raises: a header that cannot be set is a missing link, never a broken request (the same fail-open rule as every other
    seam, and more important, because this one runs inside the customer's request path).
    """
    chosen = value(environ, node=node)
    if chosen is None:
        return False
    try:
        if HEADER in headers and headers[HEADER] == chosen:
            return True                              # a client that replays its own hooks must not nest values
        headers[HEADER] = chosen
    except Exception:
        return False
    return True


def strip(headers: Any) -> list[str]:
    """Remove every `x-blackbox-*` header (case-insensitive), returning the names removed.

    The proxy has its own copy of this rule in `proxy/routes.py: is_ours`, and this one exists for the SDK side and for
    tests: two implementations of one policy is a smell, so both are asserted against the same constant here.
    """
    removed: list[str] = []
    try:
        names = [name for name in list(headers.keys()) if name.lower().startswith(PREFIX)]
        for name in names:
            try:
                del headers[name]
            except Exception:
                continue
            removed.append(name.lower())
    except Exception:
        return removed
    return removed


def parse(raw: str | None) -> tuple[str | None, int | None]:
    """`(run_id, node)` from a header value. A malformed node keeps the run: half a link beats no link."""
    if not raw:
        return None, None
    head, _, tail = raw.partition("/")
    run: str | None = head.strip() or None
    try:
        return run, int(tail) if tail.strip() else None
    except ValueError:
        return run, None
