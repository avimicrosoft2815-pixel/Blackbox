"""The Claude Code adapter (P3B-05): install, remove and inspect the `hooks` block of a settings file.

Claude Code reads its hooks from the `hooks` object in a `settings.json` (project `.claude/settings.json` or the user's
`~/.claude/settings.json`). That file belongs to the user and contains their permissions, their model, their own hooks —
so this module treats it as somebody else's document that we are allowed to add one block to, and the machinery that
makes that safe (markers, a backup, the byte-identical restore, the refusal of an unknown schema) lives in
`hooks/settings.py`, where the Codex adapter uses the same code.

What is here is the *data*: the tool's name, the two paths it documents, and §11.9's event names.
"""
from __future__ import annotations

from blackbox.hooks import events as events_mod
from blackbox.hooks import settings
from blackbox.hooks.settings import MARKER, Change  # noqa: F401  (re-exported: adapters are used through their names)

TOOL = "claude-code"
# One list for the adapter and the mapping: two spellings of an event name is how a hook stops firing.
EVENTS = events_mod.CLAUDE_EVENTS

SPEC = settings.Spec(tool=TOOL, events=EVENTS,
                     home_relative=".claude/settings.json",
                     project_relative=".claude/settings.json",
                     verified=True)


def default_path(scope: str = "project", *, home: str | None = None) -> str:
    """Where Claude Code's settings live for a scope. `project` is relative to the working directory."""
    if scope == "project":
        return SPEC.path("project")
    if scope == "user":
        return SPEC.path("user", home=home)
    raise ValueError(f"unknown scope {scope!r}; Claude Code has project and user settings")


def installed_events(body: dict) -> list[str]:          # type: ignore[type-arg]
    return settings.installed_events(body)


def install(path: str | None = None, *, scope: str = "project", now_ns: int | None = None,
            backup: bool = True) -> Change:
    return settings.install(SPEC, path, scope=scope, now_ns=now_ns, backup=backup)


def remove(path: str | None = None, *, scope: str = "project") -> Change:
    return settings.remove(SPEC, path, scope=scope)


def status(path: str | None = None, *, scope: str = "project") -> dict:   # type: ignore[type-arg]
    return settings.status(SPEC, path, scope=scope)
