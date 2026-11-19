"""The incident library: what is recorded here, in rows a person can choose from.

A capsule is evidence, and evidence a user cannot find is evidence they do not have. This module answers the question
the desktop app opens with — *what happened, and where is the one I care about* — from the **manifests only**.

Why manifests and not capsules: a capsule is thousands of frames, and a library that decompressed and hashed every one of
them to draw a list would take seconds on a directory of a hundred runs and hold the window while it did. `scan()` reads
one small JSON file per capsule through `cap.format.read_manifest`, which runs every header check the full reader runs
(so a hostile file is still refused) and reads no entry body. The consequence is stated in the row: a capsule listed here
is **unverified**, and the row says so until someone opens it and the integrity index is checked.

What a row carries, and why each part is there:

* **what it is** — capsule id, file name, run id, when it was written, how big;
* **what happened** — the recovery status and its cause, completeness, truncated tail, gaps lost from which sequence;
* **what was captured** — providers seen, models seen, whether a model call was recorded at all (an empty run is a fact,
  not a blank row);
* **what needs attention** — unreadable, truncated, incomplete, no model call, integrity not yet checked. Each reason is
  a short string the page prints; the row never invents a cause it did not read.

`scan()` is a pure function of the directory plus a size-bounded cache keyed by `(path, mtime_ns, size)`. Capsules are
written once and never modified, so the cache cannot serve a stale row except when a file is replaced in place — which is
why the key includes both mtime and size, and why `CACHE_MAX` bounds the memory.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from typing import Any

from blackbox.cap import format as cap
from blackbox.cli.cmd_ls import capsules_in

#: How many manifest reads are remembered. A capsule manifest is ~1–2 KB, so this is a few megabytes at most.
CACHE_MAX = 4000
_CACHE: dict[tuple[str, int, int], Row] = {}

#: Statuses that mean the run's evidence is complete as far as the recorder can tell. Anything else is drawn as a
#: warning, never silently smoothed over.
CLEAN_STATUSES = frozenset({"recovered", "sealed", "complete"})

SORTS = ("newest", "oldest", "largest", "status", "run")


@dataclass(frozen=True)
class Row:
    """One capsule, as the library knows it from its manifest."""

    name: str
    path: str
    bytes: int | None = None
    mtime: float | None = None
    capsule_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    status_cause: str | None = None
    completeness: str | None = None
    truncated_tail: bool | None = None
    lost_from_seq: int | None = None
    events: int | None = None
    nodes: int | None = None
    model_calls: int | None = None
    coverage: str | None = None
    gaps: int = 0
    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    capture_vantage: Any = None
    readable: bool = False
    error: str | None = None
    attention: tuple[str, ...] = ()

    @property
    def needs_attention(self) -> bool:
        return bool(self.attention)

    @property
    def when(self) -> str:
        if not self.mtime:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))

    def as_dict(self) -> dict[str, Any]:
        out = {**self.__dict__}
        out["needs_attention"] = self.needs_attention
        out["when"] = self.when
        out["providers"] = list(self.providers)
        out["models"] = list(self.models)
        out["attention"] = list(self.attention)
        return out


def _attention_of(row: Row) -> tuple[str, ...]:
    """Every reason this row deserves a second look, in the order a person would ask them."""
    reasons: list[str] = []
    if not row.readable:
        return ("unreadable: " + (row.error or "no reason given"),)
    if row.truncated_tail:
        reasons.append("the last segment was cut short")
    if row.lost_from_seq is not None:
        reasons.append(f"evidence lost from sequence {row.lost_from_seq}")
    if row.gaps:
        reasons.append(f"{row.gaps} declared gap(s)")
    if row.status not in CLEAN_STATUSES:
        cause = f" ({row.status_cause})" if row.status_cause else ""
        reasons.append(f"run status {row.status}{cause}")
    if row.events == 0:
        reasons.append("no events recorded")
    elif row.model_calls == 0:
        reasons.append("no model call was recorded")
    elif row.model_calls and not row.providers:
        reasons.append(f"{row.model_calls} model call(s), but the provider was not attributed")
    if (row.completeness or "").upper() not in ("", "FULL"):
        reasons.append(f"capture completeness {row.completeness}")
    if (row.coverage or "").upper() not in ("", "FULL"):
        reasons.append(f"capture coverage {row.coverage}")
    return tuple(reasons)


def _block(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    """A manifest sub-object, or an empty one. A capsule with a malformed block is a row with blanks, not an exception."""
    value = manifest.get(key)
    return value if isinstance(value, dict) else {}


def _row_from_manifest(path: str, manifest: dict[str, Any], *, size: int, mtime: float) -> Row:
    run_block = _block(manifest, "run")
    counts = _block(manifest, "counts")
    capture = _block(manifest, "capture")
    gaps = manifest.get("gaps")
    row = Row(
        readable=True,
        name=os.path.basename(path),
        path=path,
        bytes=size,
        mtime=mtime,
        capsule_id=manifest.get("capsule_id"),
        run_id=run_block.get("run_id"),
        status=run_block.get("status"),
        status_cause=run_block.get("status_cause"),
        completeness=run_block.get("completeness"),
        truncated_tail=run_block.get("truncated_tail"),
        lost_from_seq=run_block.get("lost_from_seq"),
        events=counts.get("events"),
        nodes=counts.get("nodes"),
        model_calls=capture.get("model_calls"),
        coverage=capture.get("coverage"),
        gaps=len(gaps) if isinstance(gaps, list) else (int(gaps) if isinstance(gaps, int) else 0),
        providers=tuple(sorted(capture.get("providers") or ())),
        models=tuple(sorted({str(m) for m in (capture.get("models") or ()) if m})),
        capture_vantage=manifest.get("capture_vantage"),
    )
    return replace(row, attention=_attention_of(row))


def _unreadable(path: str, error: str) -> Row:
    try:
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = None, None
    row = Row(name=os.path.basename(path), path=path, bytes=size, mtime=mtime, readable=False, error=error)
    return replace(row, attention=_attention_of(row))


def read_row(path: str) -> Row:
    """One row, from the manifest, cached by (path, mtime, size). Never raises for a bad capsule."""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return _unreadable(path, f"{type(exc).__name__}: {exc}")
    key = (path, stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        manifest = cap.read_manifest(path)
    except Exception as exc:                      # a broken capsule is a row with a reason, not a 500
        # `str(exc)` already carries the refusal code ("BBX-E402: …"); prefixing it again doubled it in the row.
        reason = str(exc) if hasattr(exc, "code") else f"{type(exc).__name__}: {exc}"
        row = _unreadable(path, reason)
    else:
        row = _row_from_manifest(path, manifest, size=stat.st_size, mtime=stat.st_mtime)
    if len(_CACHE) >= CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = row
    return row


def data_root(path: str) -> str:
    """The data directory a path belongs to: the path itself, or the parent of the `capsules/` holding a capsule file.

    `blackbox desktop <file.bbx>` is a documented way in, and it must open the library around that file — not a library
    of one directory that happens to be called `capsules`. A capsule copied outside a data directory keeps its own
    directory as the root, and the app then serves that single run by path.
    """
    resolved = os.path.abspath(path)
    if not os.path.isfile(resolved):
        return resolved
    parent = os.path.dirname(resolved)
    if os.path.basename(parent) == "capsules":
        return os.path.dirname(parent)
    return parent


def matches(row: Row, query: str) -> bool:
    """Substring search over the things a person types: run, capsule, provider, model, status, file name."""
    if not query:
        return True
    haystack = " ".join(str(part) for part in (
        row.name, row.capsule_id, row.run_id, row.status, row.status_cause,
        " ".join(row.providers), " ".join(row.models), row.completeness, row.coverage,
    )).lower()
    return all(term in haystack for term in query.lower().split())


@dataclass
class Library:
    data_dir: str
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 25
    query: str = ""
    status: str = ""
    sort: str = "newest"
    counts: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    providers: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.limit - 1) // self.limit)

    @property
    def page(self) -> int:
        return self.offset // self.limit + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir, "total": self.total, "offset": self.offset, "limit": self.limit,
            "query": self.query, "status": self.status, "sort": self.sort, "counts": self.counts,
            "statuses": self.statuses, "providers": self.providers, "elapsed_ms": round(self.elapsed_ms, 1),
            "rows": [row.as_dict() for row in self.rows],
        }


def _sorted(rows: list[Row], sort: str) -> list[Row]:
    if sort == "oldest":
        return sorted(rows, key=lambda r: (r.mtime or 0.0, r.name))
    if sort == "largest":
        return sorted(rows, key=lambda r: (-(r.bytes or 0), r.name))
    if sort == "status":
        return sorted(rows, key=lambda r: (str(r.status or "zz"), -(r.mtime or 0.0)))
    if sort == "run":
        return sorted(rows, key=lambda r: (str(r.run_id or "~"), r.name))
    return sorted(rows, key=lambda r: (-(r.mtime or 0.0), r.name))


def scan(data_dir: str, *, query: str = "", status: str = "", sort: str = "newest",
         limit: int = 25, offset: int = 0) -> Library:
    """Every capsule under `data_dir`, filtered, sorted and paginated. Never raises for a bad capsule."""
    started = time.perf_counter()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    sort = sort if sort in SORTS else "newest"
    paths = capsules_in(data_dir)
    rows = [read_row(path) for path in paths]
    counts = {
        "capsules": len(rows),
        "runs": len({row.run_id for row in rows if row.run_id}),
        "events": sum(int(row.events or 0) for row in rows),
        "bytes": sum(int(row.bytes or 0) for row in rows),
        "attention": sum(1 for row in rows if row.needs_attention),
    }
    statuses: dict[str, int] = {}
    providers: dict[str, int] = {}
    for row in rows:
        if not row.readable:
            statuses["unreadable"] = statuses.get("unreadable", 0) + 1
            continue
        key = str(row.status or "unknown")
        statuses[key] = statuses.get(key, 0) + 1
        for provider in row.providers or ("(none recorded)",):
            providers[provider] = providers.get(provider, 0) + 1
    filtered = [row for row in rows if matches(row, query) and (not status or str(row.status or "unknown") == status)]
    ordered = _sorted(filtered, sort)
    page = ordered[offset:offset + limit]
    return Library(data_dir=os.path.abspath(data_dir), rows=page, total=len(ordered), offset=offset, limit=limit,
                   query=query, status=status, sort=sort, counts=counts, statuses=statuses, providers=providers,
                   elapsed_ms=(time.perf_counter() - started) * 1000.0)


def find_by_name(data_dir: str, name: str) -> str | None:
    """Resolve a row's file name to a path **inside** `data_dir`. The name comes from a URL, so it is matched, not joined.

    Rows are addressed by file name rather than by capsule id because two files can hold the same capsule (a copy, a
    backup): a library that linked by id would open the first one for both rows and hide the duplicate.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    for path in capsules_in(data_dir):
        if os.path.basename(path) == name:
            return path
    return None


def find(data_dir: str, capsule_id: str) -> str | None:
    """Resolve a capsule id to a path **inside** `data_dir`. The id comes from a URL, so it is matched, not joined."""
    if not capsule_id or "/" in capsule_id or "\\" in capsule_id or capsule_id in (".", ".."):
        return None
    for path in capsules_in(data_dir):
        try:
            manifest = cap.read_manifest(path)
        except Exception:
            continue
        if manifest.get("capsule_id") == capsule_id:
            return path
    return None


def forget(path: str | None = None) -> None:
    """Drop the cache for one path (or all of it). Used by the tests, and by a caller that knows a file was replaced."""
    if path is None:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k[0] == path]:
        _CACHE.pop(key, None)
