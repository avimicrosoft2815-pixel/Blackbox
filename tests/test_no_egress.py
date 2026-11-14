"""P0-08: the core cannot talk to the network, and never may (fail-open, offline-first)."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BANNED = {
    "socket", "ssl", "http", "urllib", "urllib3", "requests", "ftplib", "telnetlib", "smtplib",
    "xmlrpc", "asyncio", "webbrowser", "httpx", "aiohttp", "socketserver", "http.client",
}
URL_MARKERS = ("http://", "https://", "ftp://")


def _files():
    return sorted(SRC.rglob("*.py"))


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
                if name.split(".")[0] in BANNED:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert not hits, "network-capable import in the core:\n" + "\n".join(hits)


def test_no_url_literals():
    hits = []
    for path in _files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(m in line for m in URL_MARKERS):
                hits.append(f"{path.relative_to(ROOT)}:{lineno} {line.strip()[:80]}")
    assert not hits, "URL literal in the core:\n" + "\n".join(hits)


def test_no_socket_module_anywhere_in_src():
    assert "socket" not in {p.name for p in SRC.rglob("*.py")}
