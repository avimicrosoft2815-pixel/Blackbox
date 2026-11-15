"""P3B-04's second half: the tapes go in, the `LINK` records come out — and nothing is written into a tape.

§11.10 puts the resolver *at recovery/seal time*, not on the hot path, and this module is where that sentence becomes
code. Three things are deliberately true here:

* **A tape is read, never edited.** `recovery.py` says "nothing here mutates a tape" and means it: a segment's bytes are
  the evidence a capsule hashes, so a resolver that appended a `LINK` to the tape it was describing would be editing its
  own evidence. The records therefore land beside the tape — as the capsule's `links.json` entry and as the answer
  `blackbox link` prints — and `links.frame_records()` exists so that a writer that *does* own a live tape (P3B-05's
  inbox, P4's viewer) can append one without going through this module.
* **What a tape says about itself is read from the tape.** The run id is the `RUN_START` payload's `run_id` when the
  tape has one and, failing that, the single `linked_run` its calls carry (the proxy's case: it is told whose run the
  socket belongs to, and records that per call). Two different linked runs on one tape is not a run id — it is two
  runs' worth of traffic through one proxy — so it resolves to `None` and the pair has to be declared, not guessed.
* **An empty answer is an answer.** `links.json` is written even when nothing linked, because "these tapes were looked
  at and nothing matched" is a different fact from "this capsule never looked", and a reader should not have to infer
  which one they have.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from typing import Any

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.rec import links
from blackbox.tape import scanner

_plat = _plat_current()

# docs/format.md, meta offset 0x03C: the four vantage points, spelled as the plan spells them.
VANTAGE_NAMES = {1: "sdk", 2: "proxy", 3: "hook", 4: "recovery"}
PROXY = "proxy"

# A declared pair is the user's statement, so it is stored where the user's statements live: one JSON file at the data
# directory's root, appended to by `blackbox link`, never rewritten by a resolver.
DECLARED_NAME = "links.declared.json"


# ── tapes → views ───────────────────────────────────────────────────────────────
def _payload(frame: dict[str, Any]) -> dict[str, Any]:
    try:
        body = cbor.decode_exact(frame.get("payload") or b"")
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _vantage_name(meta: dict[str, Any]) -> str:
    raw = meta.get("vantage")
    if not isinstance(raw, int):
        return "unknown"
    return VANTAGE_NAMES.get(raw, f"vantage-{raw}")


def view_of(scan: scanner.ScanResult) -> links.RunView:
    """One tape as the resolver sees it: who wrote it, from where, which run it says it is, and the calls it made."""
    vantage = _vantage_name(scan.meta)
    run_id: str | None = None
    linked: set[str] = set()
    calls: list[links.CallView] = []
    for frame in scan.frames:
        kind = frame.get("kind")
        if kind == C.KIND_RUN_START:
            body = _payload(frame)
            name = body.get("run_id")
            if run_id is None and isinstance(name, str) and name:
                run_id = name
        elif kind == C.KIND_MODEL_REQUEST:
            body = _payload(frame)
            fingerprint = body.get("request_fp")
            linked_run = body.get("linked_run")
            if isinstance(linked_run, str) and linked_run:
                linked.add(linked_run)
            calls.append(links.CallView(
                writer_uuid=scan.writer_uuid, vantage=vantage, wall_ns=int(frame.get("wall_ns") or 0),
                fingerprint=fingerprint if isinstance(fingerprint, str) else "",
                run_id=linked_run if isinstance(linked_run, str) else None,
                node_slot=frame.get("node_slot") if isinstance(frame.get("node_slot"), int) else None))
    if run_id is None and len(linked) == 1:
        # The proxy's case: no `RUN_START` of its own, one run id named on every call it recorded.
        run_id = next(iter(linked))
    return links.RunView.of(scan.writer_uuid, vantage, run_id, calls)


def views(directory: str | Sequence[str]) -> list[links.RunView]:
    """Every readable tape in one data directory (or across several), in `scanner.scan_all`'s stable order.

    A damaged tape is skipped, not fatal: `scanner.scan_all` already makes that choice, and a link that needed a
    damaged third tape to resolve is a link the reader should see as absent rather than as an error.
    """
    out: list[links.RunView] = []
    for one in ([directory] if isinstance(directory, str) else directory):
        for scan in scanner.scan_all(one):
            out.append(view_of(scan))
    return out


# ── the user's statements ──────────────────────────────────────────────────────
def declared_path(data_dir: str) -> str:
    return os.path.join(data_dir, DECLARED_NAME)


def declared_pairs(data_dir: str) -> list[tuple[str, str]]:
    """The pairs a user asserted in this data directory, in the order they were asserted."""
    try:
        with open(declared_path(data_dir), "rb") as fh:
            body = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    pairs: list[tuple[str, str]] = []
    rows = body.get("declared") if isinstance(body, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("a"), str) and isinstance(row.get("b"), str):
            pairs.append((row["a"], row["b"]))
    return pairs


def _pair_record(a: str, b: str, *, by: str, at_ns: int) -> dict[str, Any]:
    left, right = (a, b) if a <= b else (b, a)          # order-free: the same pair recorded twice is one pair
    return {"a": left, "b": right, "by": by, "at_ns": at_ns}


def record_declared(data_dir: str, a: str, b: str, *, by: str = "user", at_ns: int | None = None) -> dict[str, Any]:
    """Append one declared pair to `<data_dir>/links.declared.json`, atomically, and never twice.

    Idempotence is not decoration: `blackbox link a b` is a command a script may run on every start, and a resolver
    that read the same pair five times would still produce one link, but a file that grew five copies of it would make
    the *evidence* look like five assertions.
    """
    import time

    if a == b:
        raise bbx("BBX-E155", "a tape cannot declare a link to itself")
    record = _pair_record(a, b, by=by, at_ns=at_ns if at_ns is not None else time.time_ns())
    existing = declared_pairs(data_dir)
    rows: list[dict[str, Any]] = []
    path = declared_path(data_dir)
    try:
        with open(path, "rb") as fh:
            body = json.loads(fh.read().decode("utf-8"))
        if isinstance(body, dict) and isinstance(body.get("declared"), list):
            rows = [row for row in body["declared"] if isinstance(row, dict)]
    except (OSError, ValueError):
        rows = []
    if (record["a"], record["b"]) not in existing:
        rows.append(record)
    blob = json.dumps({"version": C.FORMAT_VERSION, "declared": rows}, indent=2, sort_keys=True).encode() + b"\n"
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{DECLARED_NAME}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            _plat.fdatasync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return record


# ── resolving ──────────────────────────────────────────────────────────────────
def _ordered(a: links.RunView, b: links.RunView) -> tuple[links.RunView, links.RunView]:
    """The proxy goes on the left when one side is a proxy, so a link's direction says who was watching whom."""
    if b.vantage == PROXY and a.vantage != PROXY:
        return b, a
    return a, b


def resolve_views(views_: Sequence[links.RunView], *, window_ns: int = links.DEFAULT_WINDOW_NS,
                  declared: Iterable[tuple[str, str]] = ()) -> list[links.Link]:
    """Every link the evidence supports between every *distinct* pair of writers, each pair considered once."""
    out: list[links.Link] = []
    # The same assertion recorded in two data directories is *one* assertion: `blackbox link a b` writes it to both so
    # that either capsule can see it, and a resolver that read two copies as two statements would put two identical
    # `declared` edges in the graph. Order-free, because "a linked to b" is not a direction.
    declared_pairs_ = sorted({(a, b) if a <= b else (b, a) for a, b in declared})
    for i, first in enumerate(views_):
        for second in views_[i + 1:]:
            if first.writer_uuid == second.writer_uuid:
                continue
            left, right = _ordered(first, second)
            out.extend(links.resolve(proxy=left, others=[right], window_ns=window_ns,
                                     declared_pairs=declared_pairs_))
    return out


def resolve_data_dir(data_dir: str, *, window_ns: int = links.DEFAULT_WINDOW_NS,
                     declared: Sequence[tuple[str, str]] | None = None) -> list[links.Link]:
    """Resolve the tapes of one data directory, honouring the pairs this directory's user declared."""
    pairs = declared_pairs(data_dir) if declared is None else declared
    return resolve_views(views(data_dir), window_ns=window_ns, declared=pairs)


def resolve_across(directories: Sequence[str], *, window_ns: int = links.DEFAULT_WINDOW_NS) -> list[links.Link]:
    """Resolve tapes that live in different data directories, which is what `blackbox link a b` is for."""
    pairs: list[tuple[str, str]] = []
    for directory in directories:
        pairs.extend(declared_pairs(directory))
    return resolve_views(views(directories), window_ns=window_ns, declared=pairs)


def resolve_one(data_dir: str, writer_uuid: str, *, window_ns: int = links.DEFAULT_WINDOW_NS) -> list[links.Link]:
    """The links that involve *this* tape — the question a capsule prepared from it has to answer."""
    everything = views(data_dir)
    mine = [v for v in everything if v.writer_uuid == writer_uuid]
    if not mine:
        return []
    others = [v for v in everything if v.writer_uuid != writer_uuid]
    pairs = declared_pairs(data_dir)
    out: list[links.Link] = []
    for other in others:
        left, right = _ordered(mine[0], other)
        out.extend(links.resolve(proxy=left, others=[right], window_ns=window_ns, declared_pairs=pairs))
    return out


# ── the artifact ───────────────────────────────────────────────────────────────
def document(records: Sequence[links.Link], *, window_ns: int = links.DEFAULT_WINDOW_NS,
             extra: dict[str, Any] | None = None) -> bytes:
    """The bytes of `links.json`: byte-stable across runs, sorted, with every class counted even at zero."""
    body: dict[str, Any] = {
        "version": 1,
        "window_ns": window_ns,
        "links": [record.as_dict() for record in records],
        "summary": links.summary(records),
    }
    if extra:
        body.update(extra)
    return json.dumps(body, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def for_capsule(data_dir: str, writer_uuid: str, *,
                window_ns: int = links.DEFAULT_WINDOW_NS) -> bytes:
    """The `links.json` a capsule gets: this tape's links, or the honest record that there were none."""
    return document(resolve_one(data_dir, writer_uuid, window_ns=window_ns), window_ns=window_ns)
