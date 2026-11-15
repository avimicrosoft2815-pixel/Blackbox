"""The Claude Code adapter (P3B-05): install, remove and inspect the `hooks` block of a settings file.

Claude Code reads its hooks from the `hooks` object in a `settings.json` (project `.claude/settings.json` or the user's
`~/.claude/settings.json`). That file belongs to the user and contains their permissions, their model, their own hooks —
so this module treats it as somebody else's document that we are allowed to add one block to:

* **Only our keys are touched.** Every install is scoped by a marker: our entries carry `"_blackbox": true` inside the
  command object, so a later `remove` finds them by name rather than by guessing at array positions. Any other key,
  any other hook, and every other top-level field is preserved exactly — the tests assert that a settings file with
  unrelated keys comes back from `install; remove` **byte-identical**, which is the only version of "untouched" that
  cannot be argued with.
* **A backup, once, with a timestamp.** `install` copies the file to `<name>.blackbox-<ts>.bak` before writing. `remove`
  restores from that backup **when the file still has the bytes we wrote**, which is a proof rather than a hope; if the
  user edited the file in between, the edit is kept and only our block is stripped, and the result says which of the two
  happened (`restored: "backup" | "stripped"`).
* **An unknown schema is refused.** If `hooks` (or one of its keys) is not the shape this adapter knows, it raises
  `BBX-E151` and writes nothing. A settings file is not a place to be creative: writing a block into a structure we did
  not understand is how a user loses their tool configuration.
* **`status` never writes.** It reports `installed`, the file it looked at, and the events it would hook.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any

from blackbox.errors import bbx
from blackbox.hooks import events as events_mod
from blackbox.platform import current as _plat_current

_plat = _plat_current()

TOOL = "claude-code"
MARKER = "_blackbox"
# One list for the adapter and the mapping: two spellings of an event name is how a hook stops firing.
EVENTS = events_mod.CLAUDE_EVENTS

PROJECT_RELATIVE = os.path.join(".claude", "settings.json")


def default_path(scope: str = "project", *, home: str | None = None) -> str:
    """Where Claude Code's settings live for a scope. `project` is relative to the working directory."""
    if scope == "project":
        return os.path.abspath(PROJECT_RELATIVE)
    if scope == "user":
        return os.path.join(home or os.path.expanduser("~"), ".claude", "settings.json")
    raise ValueError(f"unknown scope {scope!r}; Claude Code has project and user settings")


@dataclass
class InstallResult:
    path: str
    backup: str | None
    installed: bool
    events: list[str]
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "backup": self.backup, "installed": self.installed,
                "events": list(self.events), "changed": self.changed}


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    try:
        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Not ours to repair: a settings file that does not parse is a user problem we must not "fix" by overwriting.
        raise bbx("BBX-E151", f"{path}: {exc}") from None
    if not isinstance(body, dict):
        raise bbx("BBX-E151", f"{path}: settings must be a JSON object")
    return body


def _write(path: str, body: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    blob = json.dumps(body, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    tmp = path + ".blackbox.tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        _plat.fdatasync(fh.fileno())
    os.replace(tmp, path)


def _hooks_block(body: dict[str, Any]) -> dict[str, Any]:
    hooks = body.get("hooks", {})
    if not isinstance(hooks, dict):
        raise bbx("BBX-E151", "the `hooks` key is not an object; refusing to rewrite somebody's settings")
    return hooks


def _command(event: str) -> str:
    return f"blackbox ingest --tool {TOOL} --event {event}"


def _our_entry(event: str) -> dict[str, Any]:
    return {"matcher": "", "hooks": [{"type": "command", "command": _command(event), MARKER: True}]}


def _strip(hooks: dict[str, Any]) -> int:
    """Remove every entry this adapter added. Returns how many it removed. Mutates `hooks`."""
    removed = 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        keep = []
        for entry in entries:
            if isinstance(entry, dict) and any(isinstance(h, dict) and h.get(MARKER) for h in entry.get("hooks") or []):
                removed += 1
                continue
            keep.append(entry)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)
    return removed


def installed_events(body: dict[str, Any]) -> list[str]:
    """Which of §11.9's events this file already hooks, by our marker. Reads nothing else."""
    try:
        hooks = _hooks_block(body)
    except Exception:
        return []
    out: list[str] = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if isinstance(hook, dict) and hook.get(MARKER):
                    out.append(event)
                    break
    return sorted(set(out))


def _with_ours(body: dict[str, Any]) -> dict[str, Any]:
    """A copy of a parsed settings document with our block present exactly once per event.

    One function, used by `install` to write and by `remove` to *prove* what was written. Installing twice must not
    leave two of our entries on one event: a hook that runs twice per prompt is a bug the user pays for.
    """
    copy: dict[str, Any] = json.loads(json.dumps(body))
    hooks = _hooks_block(copy)
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise bbx("BBX-E151", f"hooks[{event!r}] is not a list; refusing to rewrite it")
        already = any(isinstance(entry, dict)
                      and any(isinstance(h, dict) and h.get(MARKER) for h in entry.get("hooks") or [])
                      for entry in entries)
        if not already:
            entries.append(_our_entry(event))
    copy["hooks"] = hooks
    return copy


def install(path: str | None = None, *, scope: str = "project", now_ns: int | None = None,
            backup: bool = True) -> InstallResult:
    """Add our block to every event in `EVENTS`, keeping everything else exactly as it was."""
    path = path or default_path(scope)
    body = _read(path)
    wanted = _with_ours(body)              # validates the schema *before* anything is copied or written
    backup_path = None
    if os.path.exists(path) and backup:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime((now_ns or time.time_ns()) / 1e9))
        backup_path = f"{path}.blackbox-{stamp}.bak"
        if not os.path.exists(backup_path):
            shutil.copy2(path, backup_path)
    _write(path, wanted)
    return InstallResult(path=path, backup=backup_path, installed=True, events=list(EVENTS),
                         changed=wanted != body)


def remove(path: str | None = None, *, scope: str = "project") -> InstallResult:
    """Take our block out again — byte-identically when the file still holds what we wrote.

    The proof is cheap and exact: if the current bytes are precisely what `install` would have produced from the
    newest backup (that is, `_serialise(backup minus our block)`), nothing else has touched the file, so restoring the
    backup returns **the original bytes**, not a re-serialisation that merely parses the same. If they differ, somebody
    edited the settings in between; their edit is kept and only our entries are stripped.
    """
    path = path or default_path(scope)
    body = _read(path)
    hooks = _hooks_block(body)
    backups = _backups(path)
    if backups:
        with open(backups[-1], "rb") as fh:
            original = fh.read()
        baseline: Any = None
        try:
            baseline = json.loads(original.decode("utf-8")) if original.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            baseline = None
        if isinstance(baseline, dict) and _serialise(_with_ours(baseline)) == _bytes_on_disk(path):
            os.replace(backups[-1], path)
            return InstallResult(path=path, backup=None, installed=False, events=[], changed=True)
    removed = _strip(hooks)
    if removed or "hooks" in body:
        if hooks:
            body["hooks"] = hooks
        else:
            body.pop("hooks", None)
        _write(path, body)
    return InstallResult(path=path, backup=backups[-1] if backups else None, installed=False,
                         events=[], changed=bool(removed))


def _bytes_on_disk(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


def status(path: str | None = None, *, scope: str = "project") -> dict[str, Any]:
    """What is installed, without writing a byte."""
    path = path or default_path(scope)
    body = _read(path)
    events = installed_events(body)
    return {"tool": TOOL, "path": path, "exists": os.path.exists(path), "installed": bool(events),
            "events": events, "missing": [e for e in EVENTS if e not in events],
            "backups": [os.path.basename(p) for p in _backups(path)]}


def _backups(path: str) -> list[str]:
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".blackbox-"
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith(prefix) and n.endswith(".bak"))
    except FileNotFoundError:
        return []
    return [os.path.join(directory, n) for n in names]


def _serialise(body: dict[str, Any]) -> bytes:
    return json.dumps(body, indent=2, sort_keys=True).encode("utf-8") + b"\n"
