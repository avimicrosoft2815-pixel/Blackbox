"""The one place in `src/` allowed to touch an HTTP library or a listening socket, declared rather than assumed.

`P0-08` and `P0-13` say the core imports stdlib only and never talks to the network. That is still true of the core —
tape, recovery, capsule, replay, CLI. It cannot be true of the capture layer, which exists to observe *other
people's* HTTP clients and to serve a loopback mock provider for tests. Rather than weaken those invariants to
"almost always", the exception is a list, in one file, with the tests enforcing three things about it:

1. **Nothing outside `cap_sdk/` and `proxy/` may be exempt.** A new network-using module has to come here and add a line, and this
   file is in the diff a reviewer reads.
2. **`httpx` may only ever be imported inside a function.** That is what makes it an *optional* integration: the
   package imports, the suite passes and `pip install blackbox-recorder` stays stdlib-only when httpx is absent.
   A module-scope `import httpx` anywhere under `cap_sdk/` fails `test_httpx_is_never_imported_at_module_scope`.
3. **The mock provider may only bind loopback.** It is a fixture, not a server: no `0.0.0.0`, no host from an
   argument, no way to make it answer a packet from off the machine.

`docs/DECISIONS.md` D-020 records the decision; `docs/compatibility.md` records what it means for a user.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Files whose third-party import is httpx, always inside a function body (optional integration).
OPTIONAL_HTTP = (
    "cap_sdk/tee.py",
    "cap_sdk/seam_httpx_hooks.py",
    "cap_sdk/seam_httpx_transport.py",
)

# Files allowed to `import socket` / `http.server`: the loopback fixture only.
LOOPBACK = ("cap_sdk/mock_provider.py",)

# The proxy (P3B-01/02) is a server *and* a client by design: it listens on loopback and connects to a configured
# upstream. It is exempt for `asyncio` and `ssl` — and for nothing else — because its reachable destinations are
# asserted to be exactly `Routes.destinations()`, in `tests/test_proxy_server.py` and `tests/test_no_egress.py`.
PROXY = ("server.py", "routes.py", "supervisor.py", "__init__.py")
PROXY_MODULES = frozenset({"asyncio", "ssl", "socket"})
# `urllib.parse` splits a base URL; `urllib.request` would fetch one, and stays banned by `test_no_egress.py`.
PROXY_DOTTED = frozenset({"urllib.parse"})


def proxy_paths() -> set[Path]:
    """The proxy package's files, which exist on disk."""
    return {SRC / "blackbox" / "proxy" / name for name in PROXY if (SRC / "blackbox" / "proxy" / name).exists()}


def exempt_paths() -> set[Path]:
    """Absolute paths of every exempt file that exists on disk."""
    return {SRC / "blackbox" / rel for rel in (*OPTIONAL_HTTP, *LOOPBACK) if (SRC / "blackbox" / rel).exists()}


def is_proxy(path: Path) -> bool:
    try:
        return path.resolve() in {p.resolve() for p in proxy_paths()}
    except OSError:                                    # pragma: no cover - a vanished file
        return False


def is_exempt(path: Path) -> bool:
    try:
        return path.resolve() in {p.resolve() for p in exempt_paths()}
    except OSError:                                    # pragma: no cover - a vanished file
        return False


def function_scoped_modules(path: Path) -> set[str]:
    """Module names imported *inside* a function in this file, i.e. deliberately deferred imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scoped: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                scoped.update(alias.name.split(".")[0] for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and not child.level:
                scoped.add(child.module.split(".")[0])
    return scoped
