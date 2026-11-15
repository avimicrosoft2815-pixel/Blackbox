"""The capture layer (P3): seams into HTTP clients, the preflight that chooses one, and the mock provider.

The design rule for this package is one sentence: **a seam either captures the bytes that were on the wire or it
does not capture, and the caller is told which.** There is no seam that "estimates" a token count, no seam that
reconstructs a stream from a parsed object, and no seam that patches something it has not recognised.

`preflight.py` is the ordered chooser (A → B → C). Every seam exposes the same three things:

* `probe()` → `(available: bool, reason: str)` — cheap, never raises, checks for the library and the shape it needs;
* `install(recorder)` → `SeamHandle` — patches, or raises `SeamUnavailable` with the reason;
* `uninstall()` — restores exactly what was patched, and is safe to call twice.

`base.py` holds the `Recorder` contract those seams call, so a new seam can be written against a stable interface —
which is also what the adapter contract in `SUPPORTED.md` documents.
"""
from __future__ import annotations

from blackbox.cap_sdk.base import ModelCall, Recorder, SeamHandle, SeamUnavailable

__all__ = ["ModelCall", "Recorder", "SeamHandle", "SeamUnavailable"]
