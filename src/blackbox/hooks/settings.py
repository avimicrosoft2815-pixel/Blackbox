"""The settings-file machinery both hook adapters share (P3B-05, P3B-06).

`claude_code.py` was written first and this module is that code with the tool-specific parts lifted into a `Spec`. The
lift is worth it for one reason: the two adapters differ **only** in a file path, a list of event names and a marker —
everything a bug lives in (the proof that a remove is byte-identical, the refusal of an unknown schema, the backup
discipline, idempotence of install) is now written once and tested by both suites. Two spellings of "restore the user's
file" is how one of them drifts.

The contract, in the order it matters:

* **Only marked entries are ours.** Every entry we add carries `_blackbox: true` inside its command object, and every
  read looks for that marker. Position, order and any other key in the file are the user's.
* **`install` validates before it writes anything** — including before it takes a backup, so a refusal leaves the
  directory exactly as it found it.
* **`remove` restores the original *bytes* when it can prove nothing else touched the file** (`_serialise(with_ours(
  backup)) == bytes_on_disk`). When it cannot, the user's edit wins and only our entries are stripped.
* **An unknown schema raises `BBX-E151` and writes nothing.** A settings file is not a place to be creative.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any

from blackbox.errors import bbx
from blackbox.platform import current as _plat_current

_plat = _plat_current()

MARKER = "_blackbox"


@dataclass(frozen=True)
class Spec:
    """Everything an adapter knows about its tool. Data, not code — that is the point of the lift."""

    tool: str
    events: tuple[str, ...]
    home_relative: str                     # e.g. ".claude/settings.json"
    project_relative: str | None = None    # None: the tool has no project scope this build knows about
    verified: bool = True                  # False when the location/schema could not be checked here (P3B-06)
    note: str = ""

    def path(self, scope: str = "project", *, home: str | None = None) -> str:
        if scope == "project" and self.project_relative:
            return os.path.abspath(self.project_relative)
        if scope in ("user", "project") and not self.project_relative and scope == "project":
            raise ValueError(f"{self.tool} has no project-scoped settings this build knows about; use --user")
        return os.path.join(home or os.path.expanduser("~"), *self.home_relative.split("/"))


@dataclass
class Change:
    path: str
    backup: str | None
    installed: bool
    events: list[str]
    changed: bool
    tool: str = ""
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool or None, "path": self.path, "backup": self.backup,
                "installed": self.installed, "events": list(self.events), "changed": self.changed,
                "schema_verified": self.verified}


# ── reading and writing the file ────────────────────────────────────────────────────────────────────────────────
def read(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    try:
        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise bbx("BBX-E151", f"{path}: {exc}") from None
    if not isinstance(body, dict):
        raise bbx("BBX-E151", f"{path}: settings must be a JSON object")
    return body


def serialise(body: dict[str, Any]) -> bytes:
    return json.dumps(body, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def write(path: str, body: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".blackbox.tmp"
    with open(tmp, "wb") as fh:
        fh.write(serialise(body))
        fh.flush()
        _plat.fdatasync(fh.fileno())
    os.replace(tmp, path)


def bytes_on_disk(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return b""


def hooks_block(body: dict[str, Any]) -> dict[str, Any]:
    hooks = body.get("hooks", {})
    if not isinstance(hooks, dict):
        raise bbx("BBX-E151", "the `hooks` key is not an object; refusing to rewrite somebody's settings")
    return hooks


def our_entry(spec: Spec, event: str) -> dict[str, Any]:
    return {"matcher": "",
            "hooks": [{"type": "command", MARKER: True,
                       "command": f"blackbox ingest --tool {spec.tool} --event {event}"}]}


def _is_ours(entry: Any) -> bool:
    return isinstance(entry, dict) and any(isinstance(hook, dict) and hook.get(MARKER)
                                          for hook in entry.get("hooks") or [])


def strip(hooks: dict[str, Any]) -> int:
    """Remove every entry this adapter added. Returns how many. Mutates `hooks`."""
    removed = 0
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        keep = [entry for entry in entries if not _is_ours(entry)]
        removed += len(entries) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)
    return removed


def installed_events(body: dict[str, Any]) -> list[str]:
    """Which events this file already hooks, by our marker. Reads nothing else and never raises."""
    try:
        hooks = hooks_block(body)
    except Exception:
        return []
    out: list[str] = []
    for event, entries in hooks.items():
        if isinstance(entries, list) and any(_is_ours(entry) for entry in entries):
            out.append(event)
    return sorted(set(out))


def with_ours(spec: Spec, body: dict[str, Any]) -> dict[str, Any]:
    """A copy with our block present exactly once per event. Used to write, and to prove what was written."""
    copy: dict[str, Any] = json.loads(json.dumps(body))
    hooks = hooks_block(copy)
    for event in spec.events:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise bbx("BBX-E151", f"hooks[{event!r}] is not a list; refusing to rewrite it")
        if not any(_is_ours(entry) for entry in entries):
            entries.append(our_entry(spec, event))
    copy["hooks"] = hooks
    return copy


def backups(path: str) -> list[str]:
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".blackbox-"
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith(prefix) and n.endswith(".bak"))
    except FileNotFoundError:
        return []
    return [os.path.join(directory, n) for n in names]


# ── the three verbs ─────────────────────────────────────────────────────────────────────────────────────────────
def install(spec: Spec, path: str | None = None, *, scope: str = "project", now_ns: int | None = None,
            backup: bool = True) -> Change:
    path = path or spec.path(scope)
    body = read(path)
    wanted = with_ours(spec, body)          # validates before anything is copied or written
    backup_path = None
    if os.path.exists(path) and backup:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime((now_ns or time.time_ns()) / 1e9))
        backup_path = f"{path}.blackbox-{stamp}.bak"
        if not os.path.exists(backup_path):
            shutil.copy2(path, backup_path)
    write(path, wanted)
    return Change(path=path, backup=backup_path, installed=True, events=list(spec.events),
                  changed=wanted != body, tool=spec.tool, verified=spec.verified)


def remove(spec: Spec, path: str | None = None, *, scope: str = "project") -> Change:
    path = path or spec.path(scope)
    body = read(path)
    hooks = hooks_block(body)
    kept = backups(path)
    if kept:
        with open(kept[-1], "rb") as fh:
            original = fh.read()
        try:
            baseline: Any = json.loads(original.decode("utf-8")) if original.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            baseline = None
        if isinstance(baseline, dict) and serialise(with_ours(spec, baseline)) == bytes_on_disk(path):
            os.replace(kept[-1], path)      # nothing else touched it: give the user their bytes back
            return Change(path=path, backup=None, installed=False, events=[], changed=True,
                          tool=spec.tool, verified=spec.verified)
    removed = strip(hooks)
    if removed or "hooks" in body:
        if hooks:
            body["hooks"] = hooks
        else:
            body.pop("hooks", None)
        write(path, body)
    return Change(path=path, backup=kept[-1] if kept else None, installed=False, events=[],
                  changed=bool(removed), tool=spec.tool, verified=spec.verified)


def status(spec: Spec, path: str | None = None, *, scope: str = "project") -> dict[str, Any]:
    path = path or spec.path(scope)
    body = read(path)
    events = installed_events(body)
    out: dict[str, Any] = {"tool": spec.tool, "path": path, "exists": os.path.exists(path),
                           "installed": bool(events), "events": events,
                           "missing": [e for e in spec.events if e not in events],
                           "backups": [os.path.basename(p) for p in backups(path)],
                           "schema_verified": spec.verified}
    if spec.note:
        out["note"] = spec.note
    return out
