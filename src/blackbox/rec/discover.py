"""P2-05 — scan the data directory and return a deterministic work queue.

Ordering is by `(created_wall_ns, writer_uuid)` so two runs on the same directory produce the same queue and a
resumed recovery does the same thing in the same order. Nothing is modified here: classification only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from blackbox import constants as C
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import repair, segments, seal as seal_mod


@dataclass
class TapeEntry:
    writer_uuid: str
    tape_dir: str
    created_wall_ns: int
    active_segment: int
    head_gen: int
    segments: list[int] = field(default_factory=list)
    sealed: list[int] = field(default_factory=list)
    orphan_segment: int | None = None
    missing_seals: list[int] = field(default_factory=list)
    damaged: list[int] = field(default_factory=list)
    stray_tmp: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "writer_uuid": self.writer_uuid, "active_segment": self.active_segment, "head_gen": self.head_gen,
            "segments": self.segments, "sealed": self.sealed, "orphan_segment": self.orphan_segment,
            "missing_seals": self.missing_seals, "damaged": self.damaged, "stray_tmp": self.stray_tmp,
            "actions": self.actions,
        }


def _stray_files(tape_dir: str) -> list[str]:
    out = []
    try:
        for name in sorted(os.listdir(tape_dir)):
            if name.endswith(".tmp") or name.endswith(".stale"):
                out.append(name)
    except FileNotFoundError:
        pass
    return out


def classify_tape(tape_dir: str) -> TapeEntry | None:
    try:
        with open(os.path.join(tape_dir, "meta"), "rb") as fh:
            meta = meta_mod.decode(fh.read(C.META_BYTES))
    except (OSError, Exception):
        return None
    head = head_mod.read_best(os.path.join(tape_dir, "head"))
    if head is None:
        return None
    segs = segments.list_segments(tape_dir)
    entry = TapeEntry(writer_uuid=os.path.basename(tape_dir.rstrip("/")), tape_dir=tape_dir,
                      created_wall_ns=meta["created_wall_ns"], active_segment=head.active_segment,
                      head_gen=head.gen, segments=segs, stray_tmp=_stray_files(tape_dir))
    for n in segs:
        state = repair.segment_state(tape_dir, n, segment_bytes=meta["segment_bytes"])
        if state["damaged"]:
            entry.damaged.append(n)
            continue
        if state["sealed"]:
            entry.sealed.append(n)
        elif n < head.active_segment:
            entry.missing_seals.append(n)
        if n == head.active_segment + 1 and repair.completes_roll(state):
            entry.orphan_segment = n
    if entry.orphan_segment is not None:
        entry.actions.append("adopt_orphan_segment")      # S15 completion, idempotent
    if entry.missing_seals:
        entry.actions.append("write_missing_seals")
    if entry.damaged:
        entry.actions.append("quarantine_damaged_segments")
    for n in entry.sealed:
        info = seal_mod.read(segments.seal_path(tape_dir, n))
        if info and info["reason"] == C.SEAL_REASON_ROLLED:
            continue
    return entry


def discover(data_dir: str) -> list[TapeEntry]:
    """All tapes under `<data_dir>/tape`, ordered by creation time then uuid (deterministic)."""
    base = os.path.join(data_dir, "tape")
    entries: list[TapeEntry] = []
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return entries
    for name in names:
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        entry = classify_tape(path)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: (e.created_wall_ns, e.writer_uuid))
    return entries


def partial_artifacts(data_dir: str) -> list[str]:
    """`.partial` capsules and orphaned `.tmp` blobs/segs worth deleting or resuming (P2-06 input)."""
    out: list[str] = []
    for root, dirs, files in os.walk(data_dir):
        dirs.sort()
        for name in sorted(files):
            if name.endswith((".partial", ".tmp")):
                out.append(os.path.join(root, name))
    return out
