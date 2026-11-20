"""Retention: delete the oldest sealed segments, declaring the hole before it exists (P1-09b).

The rule this file implements is the one the plan and the model both state:

    GAP committed → unlink

A reader must never be able to observe a hole it was not told about (I1). So for every segment about to be
deleted, a `GAP` frame covering its seq range is appended to the **active** segment and published first; only
then are the files removed. The `GAP` payload carries the covered range, the reason, and the chain value the
pruned segment ended on, so `verify` can walk the chain across a declared hole instead of reporting a break.

The two retention questions the plan leaves open are decided in `docs/DECISIONS.md` as D-016 (F2) and D-017 (F3)
rather than deferred:

* **F2** — an open run is never silently truncated: retention deletes whole sealed segments only, and the
  active segment is never a candidate, not even when the budget is blown. If the budget cannot be met without
  touching the active segment, retention stops and reports `under_budget=False`.
* **F3** — the `GAP` carries the chain link (`prev`), so a pruned range is a *declared* hole: `verify` accepts
  it, and a missing segment that no `GAP` covers is still corruption.

Unlink order (Windows and POSIX both): `.seg` first, then `.seal`, then the directory entry fsync. A seal that
outlives its segment for a moment is unambiguous — the head's `tail_seq` has already advanced past it — while a
segment without its seal would look sealed-unverified to a concurrent reader.
"""
from __future__ import annotations

import os
import time
from typing import Any

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.platform import current as _plat_current
from blackbox.tape import frame as frame_mod
from blackbox.tape import segments as seg_mod

_plat = _plat_current()

# Windows: an unlink against a path another handle has open fails with a sharing violation (WinError 32), not
# ENOENT. A reader holding an old sealed segment open is *expected* — the head's `tail_seq` already moved past
# it — so retention retries briefly and, if it still cannot delete, reports the file as `blocked` with the
# attempts it made. It never returns None and never pretends the segment is gone.
SHARING_VIOLATION_RETRIES = 5
SHARING_VIOLATION_DELAY_S = 0.01

# Policy names, read by whoever owns configuration (`P2-06` wires them; retention itself takes plain arguments
# so a test never has to mutate the environment to prove a rule).
MAX_BYTES_ENV = "BLACKBOX_TAPE_MAX_BYTES"
KEEP_HOURS_ENV = "BLACKBOX_TAPE_KEEP_HOURS"


def limits_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """(max_bytes, keep_hours) as configured, `None` for "no limit". Bad values raise instead of being ignored."""
    src = os.environ if env is None else env
    max_bytes = src.get(MAX_BYTES_ENV)
    keep_hours = src.get(KEEP_HOURS_ENV)
    out: dict[str, Any] = {"max_bytes": None, "keep_hours": None}
    if max_bytes is not None:
        out["max_bytes"] = int(max_bytes)
        if out["max_bytes"] < 0:
            raise ValueError(f"{MAX_BYTES_ENV} must be >= 0, got {max_bytes!r}")
    if keep_hours is not None:
        out["keep_hours"] = float(keep_hours)
        if out["keep_hours"] < 0:
            raise ValueError(f"{KEEP_HOURS_ENV} must be >= 0, got {keep_hours!r}")
    return out


def _is_sharing_violation(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in (32, 33) or exc.errno in (13, 16)


def unlink_with_retries(path: str, *, attempts: int = SHARING_VIOLATION_RETRIES,
                        delay_s: float = SHARING_VIOLATION_DELAY_S) -> dict[str, Any]:
    """Unlink `path`, tolerating a reader that still has it open (open-view handling, P1-09b).

    Returns `{"path", "ok", "attempts", "error"}`; `ok=False` means the file is *still there* and the caller
    must not claim the range was pruned. A file that is already gone is a successful no-op.
    """
    tries = 0
    last: str | None = None
    while tries < max(1, attempts):
        tries += 1
        try:
            os.unlink(path)
            return {"path": path, "ok": True, "attempts": tries, "error": None}
        except FileNotFoundError:
            return {"path": path, "ok": True, "attempts": tries, "error": None}
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if not _is_sharing_violation(exc):
                break
            if tries < attempts:
                time.sleep(delay_s)
    return {"path": path, "ok": False, "attempts": tries, "error": last}


def _segment_scan(path: str) -> tuple[list[int], list[tuple[int, int]]]:
    """(seqs, declared ranges) a segment holds, decoded and CRC-checked; stops at the first bad frame."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
    except FileNotFoundError:
        return [], []
    seqs: list[int] = []
    gaps: list[tuple[int, int]] = []
    off = C.SEGMENT_HEADER_BYTES
    while off + C.FRAME_OVERHEAD_BYTES <= len(buf):
        try:
            fields, nxt = frame_mod.decode(buf, off)
        except frame_mod.FrameError:
            break
        # The SEG_END frame is a committed frame inside this segment, so it dies with it and belongs in the
        # declared range: a GAP that stopped one seq short would leave a hole nobody announced.
        seqs.append(fields["seq"])
        if fields["kind"] == C.KIND_GAP:
            try:
                body = cbor.decode_exact(fields["payload"])
            except Exception:
                body = None
            if isinstance(body, dict) and "from" in body and "to" in body:
                gaps.append((int(body["from"]), int(body["to"])))
        if fields["kind"] == C.KIND_SEG_END:
            break
        off = nxt
    return seqs, gaps


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent ranges; a single merged range means "the pruned region is contiguous"."""
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def candidates(tape_dir: str, *, active_segment: int) -> list[dict[str, Any]]:
    """Sealed segments, oldest first, with the seq range each one holds. The active segment is never listed."""
    out: list[dict[str, Any]] = []
    for n in seg_mod.list_segments(tape_dir):
        if n == active_segment:
            continue
        if not os.path.exists(seg_mod.seal_path(tape_dir, n)):
            continue                                  # an unsealed, inactive segment is recovery's problem
        path = seg_mod.seg_path(tape_dir, n)
        seqs, gaps = _segment_scan(path)
        out.append({"segment": n, "path": path, "bytes": os.path.getsize(path),
                    "first_seq": seqs[0] if seqs else 0, "last_seq": seqs[-1] if seqs else 0,
                    "gaps": gaps, "mtime_ns": os.stat(path).st_mtime_ns})
    return out


def select(tape_dir: str, *, active_segment: int, max_bytes: int | None = None,
           keep_hours: float | None = None, now_ns: int | None = None) -> dict[str, Any]:
    """Which sealed segments to delete, and why. Pure: nothing is unlinked here.

    A segment holding a `GAP` frame is a *tombstone segment*: deleting it would delete the only record that the
    older range is a declared hole, so it is pinned. It becomes deletable only when a newer `GAP` covers the
    whole contiguous pruned prefix, which `apply` arranges.
    """
    now = time.time_ns() if now_ns is None else now_ns
    cands = candidates(tape_dir, active_segment=active_segment)
    pinned = [c for c in cands if c["gaps"]]
    deletable = [c for c in cands if not c["gaps"]]
    # The budget is a budget for the whole tape, so the active segment counts against it even though it can
    # never be a victim (F2). Otherwise `under_budget` would be a comfortable lie.
    active_path = seg_mod.seg_path(tape_dir, active_segment)
    active_bytes = os.path.getsize(active_path) if os.path.exists(active_path) else 0
    sealed = sum(c["bytes"] for c in cands)
    total = sealed + active_bytes
    by_age: list[dict[str, Any]] = []
    if keep_hours is not None:
        horizon = now - int(keep_hours * 3600 * 1e9)
        by_age = [c for c in deletable if c["mtime_ns"] < horizon]
    by_size: list[dict[str, Any]] = []
    remaining = total
    if max_bytes is not None:
        for c in sorted(deletable, key=lambda c: (c["segment"],)):
            if remaining <= max_bytes:
                break
            by_size.append(c)
            remaining -= c["bytes"]
    victims: list[dict[str, Any]] = []
    seen: set[int] = set()
    for c in sorted({c["segment"]: c for c in by_age + by_size}.values(), key=lambda c: c["segment"]):
        if c["segment"] not in seen:
            victims.append(c)
            seen.add(c["segment"])
    freed = sum(v["bytes"] for v in victims)
    chosen = {v["segment"] for v in victims}
    return {"victims": victims, "pinned": pinned, "candidates": cands, "active_segment": active_segment,
            "kept": [c for c in deletable if c["segment"] not in chosen],
            "tape_bytes": total, "sealed_bytes": sealed, "freed_bytes": freed,
            "active_bytes": active_bytes, "keep_bytes": total - freed,
            "under_budget": max_bytes is None or (total - freed) <= max_bytes,
            "max_bytes": max_bytes, "keep_hours": keep_hours, "now_ns": now}


def explain(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per segment the plan looked at, with the reason it is or is not a victim.

    A dry run that prints only "would delete 3 segments" is not a dry run a person can check: the interesting question is
    why the fourth one stays, and whether it stays because it is inside the keep window or because it holds the only
    record of an older hole. Every verdict here names its reason and the numbers it was derived from.
    """
    victims = {v["segment"]: v for v in plan["victims"]}
    pinned = {c["segment"]: c for c in plan["pinned"]}
    max_bytes = plan.get("max_bytes")
    keep_hours = plan.get("keep_hours")
    rows: list[dict[str, Any]] = []
    for cand in plan.get("candidates", []) + list(pinned.values()):
        number = cand["segment"]
        row = {"segment": number, "first_seq": cand["first_seq"], "last_seq": cand["last_seq"],
               "bytes": cand["bytes"], "verdict": "keep", "reasons": []}
        if number in pinned:
            row["verdict"] = "pinned"
            row["reasons"].append("holds a GAP tombstone: deleting it would delete the only record that this range is "
                                  "a declared hole")
        elif number in victims:
            row["verdict"] = "delete"
            if keep_hours is not None:
                horizon = plan["now_ns"] - int(keep_hours * 3600 * 1e9)
                if cand["mtime_ns"] < horizon:
                    row["reasons"].append(f"older than the {keep_hours:g} h keep window")
            if max_bytes is not None:
                row["reasons"].append(f"deleted to bring the tape under its {max_bytes} B budget "
                                      f"(tape held {plan['tape_bytes']} B in {len(plan['candidates'])} sealed segment(s))")
            if not row["reasons"]:
                row["reasons"].append("selected by the retention policy")
        else:

            if keep_hours is not None:
                row["reasons"].append(f"inside the {keep_hours:g} h keep window")
            if max_bytes is not None:
                row["reasons"].append(f"not needed to reach the {max_bytes} B budget "
                                      f"({plan['keep_bytes']} B would remain)")
            if not row["reasons"]:
                row["reasons"].append("no retention limit was given, so nothing is deleted")
        rows.append(row)
    active = plan.get("active_segment")
    if active is not None:
        rows.append({"segment": active, "first_seq": None, "last_seq": None, "bytes": plan.get("active_bytes", 0),
                     "verdict": "active", "reasons": ["the segment being written: never a deletion candidate"]})
    return sorted(rows, key=lambda row: row["segment"])


def _unlink_segment(tape_dir: str, c: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Unlink one segment's `.seg` then its `.seal`. Returns (blocked entries, whether the segment is gone)."""
    blocked: list[dict[str, Any]] = []
    seg_res = unlink_with_retries(c["path"])
    seal_res = unlink_with_retries(seg_mod.seal_path(tape_dir, c["segment"]))
    if not seg_res["ok"]:
        # The segment is still on disk, so the range is NOT pruned: the caller is told which file held it open.
        blocked.append({"segment": c["segment"], "path": seg_res["path"],
                        "attempts": seg_res["attempts"], "error": seg_res["error"]})
        return blocked, False
    if not seal_res["ok"]:
        blocked.append({"segment": c["segment"], "path": seal_res["path"], "seal_left": True,
                        "attempts": seal_res["attempts"], "error": seal_res["error"]})
    _plat.fsync_dir(tape_dir)
    return blocked, True


def apply(writer: Any, *, max_bytes: int | None = None, keep_hours: float | None = None,
          now_ns: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Commit the `GAP` that covers what is about to disappear, then unlink. Fails closed: no GAP, no unlink.

    Ordering, per group: commit `GAP` → publish → unlink `.seg` → unlink `.seal` → fsync the directory.

    Tombstone merging: if the segments going away are contiguous with the pinned tombstone segments, one `GAP`
    is committed for the whole merged span and the old tombstone segments are removed in the same pass, so the
    tombstones do not accumulate. If the prefix is not contiguous, each victim gets its own `GAP` and every
    pinned segment stays exactly where it is.
    """
    plan = select(writer.tape_dir, active_segment=writer.head.active_segment,
                  max_bytes=max_bytes, keep_hours=keep_hours, now_ns=now_ns)
    tape_dir = writer.tape_dir
    victims: list[dict[str, Any]] = plan["victims"]
    pinned: list[dict[str, Any]] = plan["pinned"]
    deleted: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    if not victims:
        return {**plan, "deleted": deleted, "blocked": blocked, "merged": False, "dry_run": dry_run}

    groups: list[tuple[list[dict[str, Any]], tuple[int, int]]] = []
    merged = False
    tent = victims + pinned
    if all(c["first_seq"] and c["last_seq"] for c in tent):
        span = (min(c["first_seq"] for c in tent), max(c["last_seq"] for c in tent))
        covered = [(c["first_seq"], c["last_seq"]) for c in tent] + [g for c in pinned for g in c["gaps"]]
        if _merge(covered) == [span]:
            groups = [(tent, span)]
            merged = bool(pinned)
        else:
            groups = [(victims, (min(c["first_seq"] for c in victims),
                                 max(c["last_seq"] for c in victims)))]
    else:
        groups = [(victims, (0, 0))]

    if dry_run:
        # A dry run must not commit anything: a `GAP` written for data that is still there would declare a hole that is
        # not one, and the next scan would report the segment as a retention hole while its frames sit on disk.
        planned = []
        for group, span in groups:
            planned.append({"from": span[0], "to": span[1], "segments": sorted(c["segment"] for c in group)})
        return {**plan, "deleted": [{**{k: c[k] for k in ("segment", "first_seq", "last_seq", "bytes")},
                                     "dry_run": True} for c in victims],
                "blocked": [], "would_declare": planned, "merged": merged, "dry_run": True}

    for group, span in groups:
        if span[0] and span[1]:
            gap = cbor.encode({"from": span[0], "to": span[1], "reason": "RETENTION",
                               "segments": sorted(c["segment"] for c in group), "prev": writer.chain_value})
            # GAP is a priority kind: the publish is immediate, and it happens BEFORE anything is unlinked.
            seq = writer.append(C.KIND_GAP, gap, priority=0)
            writer.publish()
            if seq == 0:                                   # a degraded writer never deletes anything
                return {**plan, "deleted": deleted, "blocked": blocked, "merged": merged, "dry_run": dry_run,
                        "aborted": "writer degraded: GAP not committed"}
        for c in group:
            seg_blocked, gone = _unlink_segment(tape_dir, c)
            blocked += seg_blocked
            if gone:
                deleted.append({"segment": c["segment"], "first_seq": c["first_seq"],
                                "last_seq": c["last_seq"], "bytes": c["bytes"]})
    if deleted and not dry_run:
        writer.set_tail_seq(max(writer.head.tail_seq, max(d["last_seq"] for d in deleted)))
    return {**plan, "deleted": deleted, "blocked": blocked, "merged": merged, "dry_run": dry_run}
