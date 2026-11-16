"""P3-05d: the ordered preflight A → B → C, and the honest record of which seam won and why the others did not.

The plan's Done-when is *each seam has a forced-failure test proving the next is chosen and recorded*. That is the
whole point of this module: a capture layer that silently degrades to a seam which cannot see chunks would produce
runs that look complete and are not, and the reader would never know which of the three produced the record. So
`choose()` returns a decision with a reason per seam — `installed`, `forced-failed`, `unavailable` — and the decision
is written into the run's metadata (`capture_seam`), because a capsule has to be able to say how it was captured.

Order and why:

* **A — client hooks**: one wrapped `__init__`, no transport patching. Its hook runs before the body is read, so it
  wraps the stream with the shared tee and sees every chunk. Least invasive seam that is still byte-accurate.
* **B — transport tee**: needs nothing from the SDK at all, which is what catches a library that builds its own
  client and ignores event hooks. Fatter (wraps the transport), so it is the fallback rather than the default.
* **C — SDK method patches** (P3-05c): works when the SDK owns the client and hands us nothing at the HTTP layer.
  Last, because it is the most version-sensitive — and because it is the only seam whose byte count is absent by
  construction (`partial=True`, events counted instead), which makes it worth reaching only when the others cannot.

The order is the plan's (`A → B → C`). It is not a quality ordering — A and B both see every chunk — it is an
invasiveness ordering: try the seam that touches least of the application first, and record which one won so a
capsule can never be ambiguous about how it was captured.

A seam that raises during `probe()` or `install()` is recorded as failed with its exception text and the preflight
moves on. A seam that is absent is recorded as unavailable with the reason. Nothing here raises into the caller:
if all three fail, the recorder stays installed-but-not-capturing and the caller gets `capture_seam=None` plus the
three reasons, which is a state the CLI prints instead of a stack trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackbox.cap_sdk.base import Receiver, SeamHandle


@dataclass
class SeamAttempt:
    name: str
    status: str                      # "installed" | "forced-failed" | "unavailable"
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"seam": self.name, "status": self.status, "reason": self.reason}


@dataclass
class PreflightResult:
    seam: str | None
    attempts: list[SeamAttempt] = field(default_factory=list)
    handle: SeamHandle | None = None

    @property
    def capturing(self) -> bool:
        return self.seam is not None

    def as_dict(self) -> dict[str, Any]:
        return {"capture_seam": self.seam, "attempts": [a.as_dict() for a in self.attempts]}


def default_seams() -> list[tuple[str, Any]]:
    """The ordered list, imported lazily so a machine without httpx pays nothing for this module."""
    from blackbox.cap_sdk import seam_httpx_hooks, seam_httpx_transport, seam_sdk_methods

    return [("httpx-hooks", seam_httpx_hooks), ("httpx-transport", seam_httpx_transport),
            ("sdk-methods", seam_sdk_methods)]


def _kwargs_for(module: Any, client: Any) -> dict[str, Any]:
    """Only hand `client=` to a seam that takes it: seam A installs on the client class, seam B on an instance."""
    if client is None:
        return {}
    import inspect

    try:
        parameters = inspect.signature(module.install).parameters
    except (TypeError, ValueError):                            # pragma: no cover - a seam without a signature
        return {}
    return {"client": client} if "client" in parameters else {}


#: The seam the *product* installed (`api.install_capture`). Kept here so a second installer does not patch the same
#: client class twice — two hooks on one class fire the recorder twice and the SDK then closes a stream twice. Measured,
#: not theorised: with `api.install()` and a script that installed its own seam, `openai`'s stream raised
#: "Attempted to call a sync close on an async stream" before a byte was read.
_ADOPTED: dict[str, Any] = {"seam": None, "handle": None}


def adopt(seam: str | None, handle: Any) -> None:
    """Record that the product installed this seam, so `choose()` can hand it back instead of patching again."""
    _ADOPTED["seam"], _ADOPTED["handle"] = seam, handle


def release() -> None:
    _ADOPTED["seam"], _ADOPTED["handle"] = None, None


def choose(recorder: Receiver, *, seams: list[tuple[str, Any]] | None = None,
           forced: dict[str, str] | None = None, client: Any = None) -> PreflightResult:
    """Try each seam in order. `forced` maps a seam name to a failure message, for the forced-failure tests.

    The `forced` map is not a test-only backdoor bolted on for convenience: the CLI's `doctor --capture` uses the
    same mechanism to *ask* what would happen if a seam were unavailable, and the tests use it to prove the ordering
    without uninstalling httpx.
    """
    attempts: list[SeamAttempt] = []
    forced = forced or {}
    if seams is None and _ADOPTED["handle"] is not None:
        # One patch per client class per process. The caller keeps running; its recorder is simply not the one writing,
        # and the decision says so rather than reporting a seam it did not install.
        return PreflightResult(seam=_ADOPTED["seam"], handle=_ADOPTED["handle"],
                               attempts=[SeamAttempt(_ADOPTED["seam"] or "-", "already-installed",
                                                     "the product installed this seam; a second patch would double it")])
    for name, module in (seams if seams is not None else default_seams()):
        if name in forced:
            attempts.append(SeamAttempt(name, "forced-failed", forced[name]))
            continue
        try:
            ok, reason = module.probe()
        except Exception as exc:                  # a probe that raises is a seam that failed, not a crash
            attempts.append(SeamAttempt(name, "unavailable", f"probe raised {type(exc).__name__}: {exc}"))
            continue
        if not ok:
            attempts.append(SeamAttempt(name, "unavailable", reason))
            continue
        try:
            handle = module.install(recorder, **({"client": client} if client is not None else {}))
        except Exception as exc:
            attempts.append(SeamAttempt(name, "forced-failed", f"install raised {type(exc).__name__}: {exc}"))
            continue
        attempts.append(SeamAttempt(name, "installed", reason))
        recorder.installed_seam = name
        return PreflightResult(seam=name, attempts=attempts, handle=handle)
    return PreflightResult(seam=None, attempts=attempts)
