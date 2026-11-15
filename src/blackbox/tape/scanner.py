"""Backward frame scan + salvage (P1-11, plan §8.10/§14.3).

Starts at the committed `head_off` (never at EOF), walks frames backwards via their trailers, and stops at
the first frame that does not validate. Everything before that point is returned; the tail is reported as
`truncated_tail` with the last recovered seq. Frames from sealed/earlier segments are appended in seq order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.errors import bbx
from blackbox.tape import chain as chain_mod
from blackbox.tape import frame as frame_mod
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import seal as seal_mod
from blackbox.tape import segments


@dataclass
class ScanResult:
    writer_uuid: str
    meta: dict
    head: head_mod.HeadSlot | None
    frames: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    truncated_tail: bool = False
    lost_from_seq: int | None = None
    gaps_declared: list[dict] = field(default_factory=list)   # committed `GAP` frames (declared holes)
    gaps_unreadable: list[dict] = field(default_factory=list)  # GAP frames we cannot decode: NOT declared
    segments_seen: list[int] = field(default_factory=list)
    sealed_segments: list[int] = field(default_factory=list)
    bad_segments: list[int] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def last_seq(self) -> int:
        return self.frames[-1]["seq"] if self.frames else 0

    @property
    def first_seq(self) -> int:
        return self.frames[0]["seq"] if self.frames else 0

    def completeness(self) -> str:
        if not self.frames:
            return "empty"
        if self.gaps:
            return "gapped"
        if self.truncated_tail:
            return "partial"
        return "full"

    def summary(self) -> dict:
        return {
            "writer_uuid": self.writer_uuid, "frames": self.frame_count,
            "first_seq": self.first_seq, "last_seq": self.last_seq,
            "truncated_tail": self.truncated_tail, "lost_from_seq": self.lost_from_seq,
            "gaps": self.gaps, "declared_gaps": self.gaps_declared,
            "unreadable_gaps": self.gaps_unreadable,
            "segments": self.segments_seen,
            "sealed": self.sealed_segments, "completeness": self.completeness(),
            "head_seq": self.head.head_seq if self.head else None,
        }


def _seg_paths(tape_dir: str) -> list[str]:
    return [p for p in (segments.seg_path_any(tape_dir, n) for n in segments.list_segments(tape_dir)) if p]


def _segment_has_frames(path: str) -> bool:
    """Content check, not a size check: every segment is preallocated to its full size, so size proves nothing."""
    try:
        head = segments.logical_bytes(path)[C.SEGMENT_HEADER_BYTES:C.SEGMENT_HEADER_BYTES + C.FRAME_OVERHEAD_BYTES]
    except OSError:
        return False
    if len(head) < C.FRAME_OVERHEAD_BYTES:
        return False
    try:
        frame_mod.decode(head, 0)
    except frame_mod.FrameError:
        return False
    return True


def _read_file(path: str, limit: int | None = None) -> bytes:
    """Logical bytes: a compacted segment is decompressed, so every caller above sees the same frame region."""
    return segments.logical_bytes(path, limit=limit)


def scan(data_dir: str, writer_uuid_hex: str, *, max_frames: int = 2_000_000) -> ScanResult:
    tape_dir = os.path.join(data_dir, "tape", writer_uuid_hex)
    try:
        with open(os.path.join(tape_dir, "meta"), "rb") as fh:
            meta = meta_mod.decode(fh.read(C.META_BYTES))
    except FileNotFoundError:
        # a writer creates the directory, then `meta`, then `head`. A reader can arrive in between; an
        # uninitialised tape is empty, and it is only an error when frames already exist next to it.
        if any(_segment_has_frames(p) for p in _seg_paths(tape_dir)):
            raise bbx("BBX-E222", f"{tape_dir}: segments exist without a meta page") from None
        return ScanResult(writer_uuid=writer_uuid_hex, meta={}, head=None)
    head = head_mod.read_best(os.path.join(tape_dir, "head"))
    segs = segments.list_segments(tape_dir)
    result = ScanResult(writer_uuid=writer_uuid_hex, meta=meta, head=head, segments_seen=segs)
    if head is None:
        # no committed head yet: empty when nothing was written, otherwise the reader cannot trust anything
        if any(_segment_has_frames(p) for p in _seg_paths(tape_dir)):
            raise bbx("BBX-E220", f"{tape_dir}: both head slots are invalid and segments hold frames")
        return result

    n_active = head.active_segment
    collected: dict[int, dict] = {}

    # 1. the active segment: walk backwards from the committed cursor
    active_path = segments.seg_path(tape_dir, n_active)
    if os.path.exists(active_path):
        buf = _read_file(active_path)
        cursor = min(head.head_off, len(buf))
        tail = frame_mod.scan_backwards(buf, cursor)
        for f in tail:
            if f["seq"] <= head.head_seq:
                collected[f["seq"]] = {**f, "segment": n_active}
        if tail and cursor > 0:
            last_off = tail[-1]["offset"] + tail[-1]["len"]
            if last_off != cursor:
                result.truncated_tail = True
                result.lost_from_seq = head.head_seq
        elif cursor > C.SEGMENT_HEADER_BYTES and not tail:
            result.truncated_tail = True
            result.lost_from_seq = head.head_seq

    # 2. earlier segments, oldest first so seq order is stable
    for n in sorted(segs):
        if n >= n_active:
            continue
        path = segments.seg_path_any(tape_dir, n)
        if path is None:
            result.bad_segments.append(n)
            continue
        seal = seal_mod.read(segments.seal_path(tape_dir, n))
        try:
            buf = _read_file(path)
        except OSError:
            result.bad_segments.append(n)
            continue
        if seal is not None:
            body_end = C.SEGMENT_HEADER_BYTES + seal["body_len"]
            result.sealed_segments.append(n)
            if len(buf) < body_end:          # logical length: a compacted segment is smaller on disk (P1-13)
                result.bad_segments.append(n)
                continue
        else:
            body_end = len(buf)
        off = C.SEGMENT_HEADER_BYTES
        while off + C.FRAME_OVERHEAD_BYTES <= body_end and len(collected) < max_frames:
            try:
                fields, nxt = frame_mod.decode(buf, off)
            except frame_mod.FrameError:
                break
            if fields["seq"] <= head.head_seq:
                collected[fields["seq"]] = {**fields, "segment": n}
            off = nxt

    result.frames = [collected[k] for k in sorted(collected)]

    # 3. declared holes: a committed `GAP` frame says "this range is gone, and here is the chain link".
    for f in result.frames:
        if f["kind"] != C.KIND_GAP:
            continue
        try:
            body = cbor.decode_exact(f["payload"])
        except Exception as exc:   # a GAP we cannot read is not a licence to call a hole declared
            result.gaps_unreadable.append({"seq": f["seq"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not isinstance(body, dict) or "from" not in body or "to" not in body:
            result.gaps_unreadable.append({"seq": f["seq"], "error": "not a {from,to} range"})
            continue
        result.gaps_declared.append({"seq": f["seq"], "from": int(body["from"]), "to": int(body["to"]),
                                     "reason": str(body.get("reason", "RETENTION")),
                                     "prev": body.get("prev")})

    if result.frames:
        present = set(collected)
        missing: list[int] = []
        # The lowest seq a reader can still reason about: the first recovered frame, the start of a declared
        # hole, or `tail_seq + 1` (the oldest seq the head still says a reader may expect). Without the last
        # term, a whole leading segment going missing would be invisible — the frames simply start later, and
        # that is precisely the silent hole I1 forbids.
        low = min([result.frames[0]["seq"]] + [d["from"] for d in result.gaps_declared]
                  + [head.tail_seq + 1])
        for seq in range(low, result.frames[-1]["seq"] + 1):
            if seq not in present:
                missing.append(seq)

        def reason_for(seq: int) -> str:
            for d in result.gaps_declared:
                if d["from"] <= seq <= d["to"]:
                    return str(d["reason"])
            return "MISSING"

        # Merge runs of missing seqs that carry the same reason. A run may span two declared holes (two
        # retention passes back to back), which is why the reason is decided per seq and not per run.
        if missing:
            start = prev = missing[0]
            run_reason = reason_for(start)
            for seq in missing[1:]:
                r = reason_for(seq)
                if seq == prev + 1 and r == run_reason:
                    prev = seq
                    continue
                result.gaps.append({"from": start, "to": prev, "reason": run_reason})
                start = prev = seq
                run_reason = r
            result.gaps.append({"from": start, "to": prev, "reason": run_reason})
    return result


def verify_chain(result: ScanResult, tape_dir: str) -> dict:
    """I9 over a scan result: walk the two-level chain and check that every declared hole is linked into it.

    Level 1 links the `CHAIN` frames inside a segment; level 2 links the segments through the next segment's
    header `prev_chain` (a roll replaces the running value with the seal chain, so a walk that ignored the
    header would report a break at every roll). A retention `GAP` carries `prev`; if that value is one the walk
    passes through, the hole is declared *inside* the chain. If it is not, the walk found a real break and says
    so — that is the whole difference between a tombstone and corruption.
    """
    problems: list[str] = []
    values: set[bytes] = set()
    per_segment: dict[int, list[dict]] = {}
    for f in result.frames:
        if f["kind"] == C.KIND_CHAIN:
            per_segment.setdefault(int(f.get("segment", 0)), []).append(f)
    checked = 0
    for n, chains in sorted(per_segment.items()):
        try:
            hdr = segments.read_header(tape_dir, n)
        except OSError as exc:
            problems.append(f"segment {n}: header unreadable: {exc}")
            continue
        value = bytes(hdr["prev_chain"])
        values.add(value)
        for f in chains:
            try:
                body = chain_mod.decode_payload(f["payload"])
            except Exception as exc:
                problems.append(f"seq {f['seq']}: unreadable CHAIN payload: {exc}")
                continue
            if body["prev"] != value:
                problems.append(f"seq {f['seq']}: prev {body['prev'].hex()[:16]} does not continue "
                                f"{value.hex()[:16]}")
            value = chain_mod.chain_value(value, body["batch"])
            values.add(value)
            checked += 1
    links = []
    for d in result.gaps_declared:
        linked = d["prev"] in values
        links.append({"from": d["from"], "to": d["to"], "seq": d["seq"], "linked": linked})
        if not linked:
            problems.append(f"GAP seq {d['seq']} ({d['from']}..{d['to']}) points at a chain value the walk "
                            "never passes: a hole, not a declared one")
    return {"ok": not problems, "chain_frames": checked, "segments": len(per_segment),
            "chain_values": len(values), "declared_links": links, "problems": problems}


def scan_all(data_dir: str) -> list[ScanResult]:
    base = os.path.join(data_dir, "tape")
    out: list[ScanResult] = []
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return out
    for name in names:
        if os.path.isdir(os.path.join(base, name)):
            try:
                out.append(scan(data_dir, name))
            except Exception:      # a damaged tape must not hide the others
                continue
    return out
