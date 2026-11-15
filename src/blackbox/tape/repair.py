"""Roll completion and idempotent adoption of an orphan segment (P1-09 reopen path, P2-05).

Crash stage S15 leaves this state: `SEG_END` for segment `n` is published in the head, `<n+1>.seg` exists with
a valid header and an empty body, and the head still names `n` as active. Both the writer's reopen path and
`rec/discover.py` must *adopt* `n+1` rather than refuse: first because §16.2a S15 asserts the adoption, second
because a recovery that errors out on a legal crash state is a recovery that does not recover.

Everything here is idempotent. Running it twice changes no byte and publishes no extra generation once the
head already names `n+1`.
"""
from __future__ import annotations

import os
from typing import Any

from blackbox import constants as C
from blackbox.tape import frame as frame_mod
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import seal as seal_mod
from blackbox.tape import segments


def segment_state(tape_dir: str, n: int, *, segment_bytes: int | None = None) -> dict[str, Any]:
    """Classify one segment file: missing, header-only (empty body), open, sealed, or damaged."""
    path = segments.seg_path(tape_dir, n)
    seal_path = segments.seal_path(tape_dir, n)
    info: dict[str, Any] = {"segment": n, "path": path, "exists": os.path.exists(path),
                  "sealed": os.path.exists(seal_path), "empty_body": False, "frames": 0,
                  "last_kind": None, "first_seq": None, "last_seq": None, "header": None, "damaged": False}
    if not info["exists"]:
        return info
    try:
        with open(path, "rb") as fh:
            header = segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))
            info["header"] = header
            buf = fh.read()
    except Exception:
        info["damaged"] = True
        return info
    off = 0
    size = segment_bytes or header["segment_bytes"]
    limit = min(len(buf), max(0, size - C.SEGMENT_HEADER_BYTES))
    frames = 0
    while off + C.FRAME_OVERHEAD_BYTES <= limit:
        try:
            fields, nxt = frame_mod.decode(buf, off)
        except frame_mod.FrameError:
            break
        frames += 1
        if info["first_seq"] is None:
            info["first_seq"] = fields["seq"]
        info["last_seq"] = fields["seq"]
        info["last_kind"] = fields["kind"]
        off = nxt
    info["frames"] = frames
    info["body_bytes"] = off          # committed prefix length inside the body
    info["empty_body"] = frames == 0
    return info


def completes_roll(state: dict[str, Any]) -> bool:
    """True when this segment is the `n+1` of an interrupted roll: valid header, nothing written yet."""
    return bool(state["exists"] and not state["damaged"] and state["empty_body"] and not state["sealed"])


def rebuild_head(tape_dir: str, *, sealer: bytes = b"") -> dict[str, Any] | None:
    """Reconstruct `head` from the segments when both slots are unreadable.

    The head is a *hint*: every frame carries its own `seq`, so the committed prefix is recoverable from the
    segment bytes alone. Until this existed, a kill during head creation left `segments exist but the head is
    unreadable` — a legal crash state that recovery refused (kill matrix seed 120 on tmpfs).

    Returns a report, or None when there is nothing to rebuild from.
    """
    existing = segments.list_segments(tape_dir)
    if not existing:
        return None
    with open(os.path.join(tape_dir, "meta"), "rb") as fh:
        meta = meta_mod.decode(fh.read(C.META_BYTES))
    segment_bytes = meta["segment_bytes"]
    newest = max(existing)
    state = segment_state(tape_dir, newest, segment_bytes=segment_bytes)
    if state["damaged"]:
        return None
    # continue from whatever generation is still readable, so a rebuild never publishes a *lower* gen than a
    # surviving slot (readers pick the highest gen; equal gens would be ambiguous)
    current = head_mod.read_best(os.path.join(tape_dir, "head"))
    base = head_mod.HeadSlot(gen=current.gen if current else 0, head_seq=state["last_seq"] or 0,
                             head_off=C.SEGMENT_HEADER_BYTES + state.get("body_bytes", 0),
                             tail_seq=state["last_seq"] or 0, sealed_seq=state["last_seq"] or 0,
                             active_segment=newest)
    hmap = head_mod.HeadMap(os.path.join(tape_dir, "head"))
    try:
        slot = hmap.publish(base)
        report = {"rebuilt_head": newest, "head_seq": slot.head_seq, "head_off": slot.head_off,
                  "frames": state["frames"], "head_gen_after": slot.gen}
    finally:
        hmap.close()
    return report


def adopt_orphan(tape_dir: str, *, sealer: bytes = b"", closed_wall_ns: int | None = None) -> dict[str, Any] | None:
    """Complete an interrupted roll. Returns a report, or None when there is nothing to do.

    Steps (each skipped when already done): seal segment `n` if it is missing its `.seal`, then publish the
    head with `active_segment = n+1` and `head_off = 4096`. The `.seal` is written before the head moves, so a
    crash in the middle of adoption leaves the same state it found.
    """
    with open(os.path.join(tape_dir, "meta"), "rb") as fh:
        meta = meta_mod.decode(fh.read(C.META_BYTES))
    segment_bytes = meta["segment_bytes"]
    head = head_mod.read_best(os.path.join(tape_dir, "head"))
    if head is None:
        return None
    n = head.active_segment
    nxt = n + 1
    current = segment_state(tape_dir, n, segment_bytes=segment_bytes)
    candidate = segment_state(tape_dir, nxt, segment_bytes=segment_bytes)
    if not completes_roll(candidate):
        return None
    if current["damaged"]:
        return None
    # The body to seal is exactly the committed prefix `[4096, head_off)`. It normally ends with `SEG_END`,
    # but roll() skips that frame when it cannot fit, so requiring it here would refuse a legal crash state
    # (found by the randomized kill matrix, seed 352). Anything past `head_off` is uncommitted and is not
    # covered by the seal.
    start = C.SEGMENT_HEADER_BYTES
    if not (start <= head.head_off <= (head.head_off + segment_bytes)):
        return None
    if head.head_off < start or head.head_off > segment_bytes:
        return None
    report = {"adopted": nxt, "sealed": None, "head_gen_before": head.gen}
    seal_path = segments.seal_path(tape_dir, n)
    if not os.path.exists(seal_path):
        with open(current["path"], "rb") as fh:
            fh.seek(start)
            body = fh.read(head.head_off - start)
        info = seal_mod.write(seal_path, segment_no=n, reason=C.SEAL_REASON_RECOVERED,
                              start_seq=current["first_seq"] or 0, end_seq=current["last_seq"] or 0,
                              frame_count=current["frames"], body=body,
                              prev_chain=current["header"]["prev_chain"], sealer=sealer,
                              closed_wall_ns=closed_wall_ns)
        report["sealed"] = n
        report["seal_chain"] = info["chain"].hex()
    # move the head (idempotent: if it already names n+1 this is skipped)
    if head.active_segment != nxt:
        hmap = head_mod.HeadMap(os.path.join(tape_dir, "head"))
        try:
            slot = head_mod.HeadSlot(gen=head.gen, head_seq=head.head_seq, head_off=C.SEGMENT_HEADER_BYTES,
                                     tail_seq=head.tail_seq, sealed_seq=head.head_seq,
                                     active_segment=nxt, flags=head.flags & ~C.HEAD_FLAG_ROLL_PENDING,
                                     dropped_events=head.dropped_events)
            hmap.publish(slot)
            report["head_gen_after"] = slot.gen
        finally:
            hmap.close()
    return report
