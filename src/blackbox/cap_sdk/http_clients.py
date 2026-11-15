"""Which HTTP client modules this machine actually has, and which one a given stream came from.

The fact that forced this file: the published `openai` 3.51.0 and `anthropic` 2.8.0 speak **`httpx2`**, not `httpx`.
A seam that patches `httpx.Client` installs successfully, reports `installed`, and captures nothing at all for those
SDKs — the worst possible failure for a recorder, because the run looks recorded. Seams A and B therefore patch every
client module that is present, and the preflight's decision records which modules a seam actually covers.

`httpx2` mirrors the httpx API this layer relies on (`Client`, `AsyncClient`, `HTTPTransport`,
`AsyncHTTPTransport`, `SyncByteStream`, `AsyncByteStream`, and the same `isinstance` assertion on the response
stream), so one implementation covers both — but the *base class* of a tee must come from the same module as the
stream it wraps, because that assertion is `isinstance` against the module's own class. Hence `module_of_stream`.
"""
from __future__ import annotations

import importlib
from typing import Any

# Order matters only for reporting: the first module is the one a reader is most likely to recognise.
KNOWN = ("httpx", "httpx2")


def available() -> list[tuple[str, Any]]:
    """Every known client module that imports on this machine, with its name."""
    found: list[tuple[str, Any]] = []
    for name in KNOWN:
        try:
            found.append((name, importlib.import_module(name)))
        except Exception:                              # absent, or broken: not this layer's problem to raise
            continue
    return found


def module_of_stream(stream: Any) -> Any:
    """The client module whose `ByteStream` base class `stream` belongs to.

    Falls back to the first available module when the stream's module is unknown (a plain iterator handed in by a
    test, for instance) — the tee only needs *a* valid base class in that case, and the bytes are unaffected.
    """
    root = type(stream).__module__.split(".")[0]
    try:
        return importlib.import_module(root)
    except Exception:
        found = available()
        if not found:                                  # pragma: no cover - no client module at all
            raise RuntimeError("no known HTTP client module is importable") from None
        return found[0][1]
