"""The Codex CLI adapter (P3B-06) — with its schema labelled unverified, in the product surface, not just in a doc.

Codex's hook settings were **not** verifiable from this build's sandbox: the adapter's location and the shape of the
block it writes are an assumption, and `SPEC.verified = False` says so — `blackbox hook status --codex` prints
`schema_verified: false`, and `docs/STATUS.md` labels the row instead of claiming a verification that did not happen.
What *is* verified is everything the mistake would cost:

* **Being wrong about an event name costs a `MARK`, not an event.** The adapter installs twelve names; anything the tool
  fires that is not one of them is written as `MARK{kind: "tool_event", raw_name}` by the folder, so a wrong guess
  shows up as a visible fact rather than as missing data (that is `hooks/events.py`'s `_codex` table).
* **Being wrong about the location costs a refusal, not a corrupted config.** An unknown schema raises `BBX-E151`
  and writes nothing; `--settings <path>` exists so the first real user can point the adapter at whatever Codex
  actually reads without waiting for a release.
* **Being wrong about the file costs the user nothing.** `remove` restores the original bytes when it can prove
  nothing else touched the file, and strips only our marked entries when it cannot — the same code path the Claude
  adapter has had since P3B-05, because both go through `hooks/settings.py`.

The twelve names are the adapter's **contract with the folder**, not a claim about the tool: `session.start`,
`session.end`, `turn.start`, `turn.end`, `user.prompt`, `tool.call`, `tool.result`, `tool.error`, `agent.message`,
`context.compact`, `notify`, `error`.
"""
from __future__ import annotations

from blackbox.hooks import events as events_mod
from blackbox.hooks import settings
from blackbox.hooks.settings import MARKER, Change  # noqa: F401  (re-exported for symmetry with claude_code)

TOOL = "codex"
EVENTS = events_mod.CODEX_EVENTS

NOTE = ("location and schema are an assumption in this build: they could not be verified against Codex's own "
        "documentation here, which is why this adapter refuses any `hooks` block it does not recognise and why "
        "`--settings <path>` exists. An unknown event name is not lost — the folder writes it as MARK{tool_event}.")

SPEC = settings.Spec(tool=TOOL, events=EVENTS,
                     home_relative=".codex/settings.json",
                     project_relative=".codex/settings.json",
                     verified=False, note=NOTE)


def default_path(scope: str = "project", *, home: str | None = None) -> str:
    if scope == "project":
        return SPEC.path("project")
    if scope == "user":
        return SPEC.path("user", home=home)
    raise ValueError(f"unknown scope {scope!r}; use project or user")


def installed_events(body: dict) -> list[str]:          # type: ignore[type-arg]
    return settings.installed_events(body)


def install(path: str | None = None, *, scope: str = "project", now_ns: int | None = None,
            backup: bool = True) -> Change:
    return settings.install(SPEC, path, scope=scope, now_ns=now_ns, backup=backup)


def remove(path: str | None = None, *, scope: str = "project") -> Change:
    return settings.remove(SPEC, path, scope=scope)


def status(path: str | None = None, *, scope: str = "project") -> dict:   # type: ignore[type-arg]
    return settings.status(SPEC, path, scope=scope)
