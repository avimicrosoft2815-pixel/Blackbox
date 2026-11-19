"""P0-08: the core cannot talk to the network, and never may (fail-open, offline-first).

The capture layer is the declared exception (D-020, `_capture_policy.py`): the seams import httpx inside functions
because their whole job is to sit inside a client the application already built, and the mock provider binds loopback
because the gate needs a real socket that is not the internet. Both are narrow, both are listed, and the loopback
check below is what keeps the second one honest.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import _capture_policy

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BANNED = {
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "ftplib", "telnetlib", "smtplib",
    "xmlrpc", "asyncio", "webbrowser", "httpx", "aiohttp", "socketserver", "http.client",
}
URL_MARKERS = ("http://", "https://", "ftp://")
WILDCARD_BIND = ".".join(["0"] * 4)      # not written as a literal: this file forbids it (ruff S104)
LOOPBACK_URLS = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?")
PROVIDER_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|\[::1\])[A-Za-z0-9.-]+")


def _files():
    return sorted(SRC.rglob("*.py"))


def _allowed_import(path: Path, name: str) -> bool:
    """Two narrow permissions, both listed in `_capture_policy.py` and both checked for scope, not just presence.

    * the seam/tee files may import `httpx`, but only inside a function — that is what keeps it optional;
    * the mock provider and the two loopback page servers may import `socket`/`http.server`, because they *are* servers,
      on loopback; the servers may also use `urllib.parse`, which parses a path and cannot fetch one.
    """
    root = name.split(".")[0]
    if _capture_policy.is_server(path):
        if name in _capture_policy.SERVER_DOTTED:
            return True
        return name.split(".")[0] in _capture_policy.SERVER_MODULES
    if _capture_policy.is_proxy(path):
        # `urllib.parse` is a parser; every other `urllib` submodule, and the root, stay banned below.
        if name in _capture_policy.PROXY_DOTTED:
            return True
        return root in _capture_policy.PROXY_MODULES
    if not _capture_policy.is_exempt(path):
        return False
    if path.name == "mock_provider.py":
        return root in {"socket", "http", "httpx"}
    return root == "httpx" and root in _capture_policy.function_scoped_modules(path)


def test_forbidden_imports():
    hits = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in BANNED and not _allowed_import(path, name):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert not hits, "network-capable import in the core:\n" + "\n".join(hits)


def test_no_url_literals():
    hits = []
    for path in _files():
        proxy_loopback = _capture_policy.is_proxy(path)
        loopback_ok = _capture_policy.is_exempt(path) and path.name == "mock_provider.py"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if not any(m in line for m in URL_MARKERS):
                continue
            if loopback_ok and "http://127.0.0.1" in line and "https://" not in line:
                continue
            # The proxy may name loopback (its base URL) and nothing else: a hardcoded provider URL inside
            # `proxy/` would be a destination outside the configured table, which is the whole point of the table.
            if proxy_loopback and LOOPBACK_URLS.search(line) and not PROVIDER_URL.search(line):
                continue
            hits.append(f"{path.relative_to(ROOT)}:{lineno} {line.strip()[:80]}")
    assert not hits, "URL literal in the core:\n" + "\n".join(hits)


def test_no_socket_module_anywhere_in_src():
    assert "socket" not in {p.name for p in SRC.rglob("*.py")}


def test_the_mock_provider_can_only_bind_loopback():
    """It is a fixture for the gate, not a server: no `0.0.0.0`, no configured host, no way to answer the LAN."""
    source = (SRC / "blackbox" / "cap_sdk" / "mock_provider.py").read_text(encoding="utf-8")
    assert WILDCARD_BIND not in source
    assert '("127.0.0.1", 0)' in source, "the mock provider must bind an ephemeral loopback port"


def test_the_proxy_can_only_reach_its_configured_upstreams():
    """P3B-02: the allowlist is the route table. `open_connection` has one call site in `proxy/server.py` and it is
    handed `route.destination`; the destinations are therefore a *computable* set, and this is the assertion that the
    server has no second source for a host."""
    source = (SRC / "blackbox" / "proxy" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "open_connection"]
    assert len(calls) == 1, f"proxy/server.py opens {len(calls)} connections; exactly one is expected"
    call = calls[0]
    targets = [node for node in ast.walk(call) if isinstance(node, (ast.Name, ast.Attribute))]
    assert any(getattr(node, "attr", getattr(node, "id", "")) == "destination" for node in targets), \
        "the only outbound connection must be handed a route's destination, not a computed host"

    from blackbox.proxy.routes import Routes

    routes = Routes.from_specs(["openai=https://api.openai.com", "local=http://127.0.0.1:8000/v1@/local"],
                               default="openai")
    assert routes.destinations() == {("api.openai.com", 443), ("127.0.0.1", 8000)}
