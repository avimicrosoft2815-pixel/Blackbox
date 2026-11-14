"""Backward frame scan + salvage (P1-11, plan §8.10/§14.3).

Starts at the committed `head_off` (never at EOF), walks frames backwards via their trailers, and stops at
the first frame that does not validate. Everything before that point is returned; the tail is reported as
`truncated_tail` with the last recovered seq. Frames from sealed/earlier segments are appended in seq order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from blackbox import constants as C
from blackbox.errors import bbx
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
            "gaps": self.gaps, "segments": self.segments_seen,
            "sealed": self.sealed_segments, "completeness": self.completeness(),
            "head_seq": self.head.head_seq if self.head else None,
        }


def _seg_paths(tape_dir: str) -> list[str]:
    return [segments.seg_path(tape_dir, n) for n in segments.list_segments(tape_dir)]


def _segment_has_frames(path: str) -> bool:
    """Content check, not a size check: every segment is preallocated to its full size, so size proves nothing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(C.SEGMENT_HEADER_BYTES)
            head = fh.read(C.FRAME_OVERHEAD_BYTES)
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
    with open(path, "rb") as fh:
        return fh.read() if limit is None else fh.read(limit)


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
                collected[f["seq"]] = f
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
        path = segments.seg_path(tape_dir, n)
        if not os.path.exists(path):
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
            if os.path.getsize(path) < body_end:
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
                collected[fields["seq"]] = fields
            off = nxt

    result.frames = [collected[k] for k in sorted(collected)]

    # 3. declared gaps: any seq absent between the first and last recovered frame
    if result.frames:
        present = set(collected)
        missing: list[int] = []
        for seq in range(result.frames[0]["seq"], result.frames[-1]["seq"] + 1):
            if seq not in present:
                missing.append(seq)
        if missing:
            start = prev = missing[0]
            for seq in missing[1:]:
                if seq == prev + 1:
                    prev = seq
                    continue
                result.gaps.append({"from": start, "to": prev, "reason": "MISSING"})
                start = prev = seq
            result.gaps.append({"from": start, "to": prev, "reason": "MISSING"})
    return result


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
