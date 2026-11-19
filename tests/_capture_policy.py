"""The one place in `src/` allowed to touch an HTTP library or a listening socket, declared rather than assumed.

`P0-08` and `P0-13` say the core imports stdlib only and never talks to the network. That is still true of the core —
tape, recovery, capsule, replay, CLI. It cannot be true of the capture layer, which exists to observe *other
people's* HTTP clients and to serve a loopback mock provider for tests. Rather than weaken those invariants to
"almost always", the exception is a list, in one file, with the tests enforcing three things about it:

1. **Nothing outside `cap_sdk/`, `proxy/` and `ui/server.py` may be exempt.** A new network-using module has to come here and add a line, and this
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

# The viewer's loopback server (P4-11). It exists so a capsule can be opened in a browser that refuses `file://`; it is
# allowed `http.server`/`socket`/`socketserver` and NOTHING else — no `ssl`, no `urllib`, no client socket call. The
# scope is asserted, not assumed: `tests/test_ui_server.py` checks the bind tuple is a loopback literal, that the module
# has no `connect`/`create_connection`/`urlopen` call at all, and that no `0.0.0.0` appears in it.
SERVERS = ("ui/server.py", "ui/app.py")
SERVER_MODULES = frozenset({"http", "socket", "socketserver"})
# `urllib.parse` splits a request path; `urllib.request` would fetch one and stays banned. Same permission the proxy has.
SERVER_DOTTED = frozenset({"urllib.parse"})

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


def server_paths() -> set[Path]:
    """The viewer server files, which exist on disk."""
    return {SRC / "blackbox" / rel for rel in SERVERS if (SRC / "blackbox" / rel).exists()}


def is_server(path: Path) -> bool:
    try:
        return path.resolve() in {p.resolve() for p in server_paths()}
    except OSError:                                    # pragma: no cover - a vanished file
        return False


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


#: The **desktop shell** is the second declared exception, and it is narrower than the first: it may name the window
#: toolkit, and only inside functions, so `import blackbox` never needs it.
#:
#: A window needs a webview, and no webview is in the standard library. The alternative to this list is either a second
#: UI in a toolkit that ships with Python (a second design to keep honest, and a worse one) or a bundled browser engine
#: (a 100 MB download for a page that is already tested). So: one file, two module names, deferred imports, and three
#: tests that hold it — `test_only_stdlib_imports` allows these names only in this file, `test_the_desktop_shell_imports_
#: the_window_only_inside_functions` fails a module-scope import, and `test_importing_the_package_needs_no_window_toolkit`
#: proves `import blackbox` still works on a machine that has neither.
OPTIONAL_DESKTOP = {"blackbox/ui/desktop.py": {"webview", "gi"}}


def function_scoped_desktop_modules(path: Path) -> set[str]:
    """The names `OPTIONAL_DESKTOP` grants this file, but only the ones it actually imports inside a function."""
    try:
        rel = path.resolve().relative_to(SRC.resolve()).as_posix()      # e.g. "blackbox/ui/desktop.py"
    except (OSError, ValueError):
        return set()
    names = OPTIONAL_DESKTOP.get(rel, set())
    return names & function_scoped_modules(path)


def module_scoped_desktop_imports(path: Path) -> set[str]:
    """The names from `OPTIONAL_DESKTOP` this file imports at module scope — every one of these is a bug."""
    try:
        rel = path.resolve().relative_to(SRC.resolve()).as_posix()      # e.g. "blackbox/ui/desktop.py"
    except (OSError, ValueError):
        return set()
    names = OPTIONAL_DESKTOP.get(rel, set())
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in tree.body:                        # the module's own statements, not a function's
        if isinstance(node, ast.Import):
            out.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out & names
