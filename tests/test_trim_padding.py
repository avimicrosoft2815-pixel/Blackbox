"""P4-21 — a capsule carries the segment's frames, not its preallocated hole.

A tape segment is 16 MiB of preallocated file with a few KiB of frames at the front, and recovery used to put the whole
file into the capsule: every five-frame run produced two 16 MiB capsules, so a demo filled a disk with holes and `ls`
reported 16.0 MiB for a run whose evidence was 4 KiB. `recovery.trim_padding` drops the all-zero tail and stops on the
last complete frame boundary.

The tests that matter are the two a trim can quietly get wrong. A frame may **end in zero bytes** — a naive zero-trim
cuts into it and silently loses the last frame, which is exactly what the first version of this change did (`graph.json`
declared 5 frames, the capsule recomputed 4). And a **non-zero tail is evidence**: a partially written frame or damage
must stay in the capsule, because the manifest's `completeness`/`truncated_tail` describe it and a reader may need those
bytes. A trimmer that drops "whatever does not parse" would be deleting the wreckage the capsule exists to preserve.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.cap import derived
from blackbox.cap import format as cap
from blackbox.cap import manifest as manifest_mod
from blackbox.cli import cmd_ls
from blackbox.codec import cbor
from blackbox.rec import recovery
from blackbox.tape import frame as frame_mod
from blackbox.tape import segments
from blackbox.tape import writer as writer_mod

RUN_ID = "r_0123456789abcdef"


def _tape(tmp_path: Path, *, events: int = 6, tail: bytes = b"") -> tuple[Path, str]:
    data = tmp_path / "data"
    writer = writer_mod.TapeWriter(str(data), mode="process", vantage=1,
                                   segment_bytes=C.MIN_SEGMENT_BYTES).open()
    writer.emit(C.KIND_RUN_START, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    for _ in range(events):
        writer.emit(C.KIND_MODEL_REQUEST, b'{"m":"model"}', node_slot=1, run_slot=1)
    if tail:
        writer.emit(C.KIND_MODEL_REQUEST, tail, node_slot=1, run_slot=1)
    writer.emit(C.KIND_RUN_END, cbor.encode({"run_id": RUN_ID}), node_slot=0)
    writer.close()
    return data, writer.writer_uuid.hex()


def _capsule(tmp_path: Path, **kwargs: object) -> tuple[Path, dict]:
    data, uuid_hex = _tape(tmp_path, **kwargs)  # type: ignore[arg-type]
    result = recovery.recover(str(data), uuid_hex)
    assert result is not None and result.capsule_path
    return Path(result.capsule_path), cap.read_capsule(result.capsule_path).manifest


def _tape_dir(data: Path, uuid_hex: str) -> Path:
    return data / "tape" / uuid_hex


def _segments(tape_dir: Path) -> list[bytes]:
    return [segments.logical_bytes(segments.seg_path_any(str(tape_dir), number))
            for number in segments.list_segments(str(tape_dir))]


def _decoded(raw: bytes) -> list[dict]:
    out, off = [], C.SEGMENT_HEADER_BYTES
    while off < len(raw):
        try:
            fields, nxt = frame_mod.decode(raw, off)
        except Exception:
            break
        out.append(fields)
        off = nxt
    return out


def _last_end(raw: bytes) -> int:
    end, off = C.SEGMENT_HEADER_BYTES, C.SEGMENT_HEADER_BYTES
    while off < len(raw):
        try:
            _fields, nxt = frame_mod.decode(raw, off)
        except Exception:
            break
        end = off = nxt
    return end


def _segment_entries(capsule: cap.Capsule) -> list[bytes]:
    return [blob for name, blob in capsule.entries.items() if name.startswith("events/seg-")]


# ── the win ──────────────────────────────────────────────────────────────────
def test_the_hole_is_gone_and_the_frames_are_all_there(tmp_path: Path) -> None:
    capsule_path, manifest = _capsule(tmp_path)
    capsule = cap.read_capsule(str(capsule_path))
    raws = _segments(next((tmp_path / "data" / "tape").iterdir()))
    assert raws and all(len(raw) == C.MIN_SEGMENT_BYTES for raw in raws)
    entries = _segment_entries(capsule)
    assert len(entries) == len(raws)
    for raw, entry in zip(raws, entries, strict=True):
        assert len(entry) < len(raw) // 4, "a capsule entry should not be a copy of the preallocated segment"
        assert len(entry) <= 64 * 1024
        assert [frame["seq"] for frame in _decoded(entry)] == [frame["seq"] for frame in _decoded(raw)]
    # and the capsule's own frame reader agrees with the tape it was trimmed from
    assert len(derived.frames_of(capsule)) >= manifest["counts"]["events"]


def test_a_frame_that_ends_in_zero_bytes_keeps_its_tail(tmp_path: Path) -> None:
    data, uuid_hex = _tape(tmp_path, tail=b'{"tail":1}' + b"\x00" * 8)
    raw = _segments(_tape_dir(data, uuid_hex))[0]
    trimmed = recovery.trim_padding(raw)
    assert len(trimmed) < len(raw)
    assert [frame["seq"] for frame in _decoded(trimmed)] == [frame["seq"] for frame in _decoded(raw)]
    assert len(_decoded(trimmed)) >= 9            # run start + 6 events + the zero-tailed frame + run end + bookkeeping
    assert trimmed.endswith(b"\x00" * 4096) is False, "the padding goes, the frame's own zero bytes stay"


# ── what must not be dropped ────────────────────────────────────────────────
def test_damage_past_the_last_frame_is_kept(tmp_path: Path) -> None:
    data, uuid_hex = _tape(tmp_path)
    raw = bytearray(_segments(_tape_dir(data, uuid_hex))[0])
    last_end = _last_end(bytes(raw))
    assert last_end < len(raw)
    raw[last_end:last_end + 2] = b"\xde\xad"          # a torn write after the last whole frame
    trimmed = recovery.trim_padding(bytes(raw))
    assert b"\xde\xad" in trimmed, "damage is evidence; the trim must not delete it"
    assert len(trimmed) < len(raw), "but the padding behind it still goes"


def test_a_segment_with_no_frames_is_cut_to_its_header(tmp_path: Path) -> None:
    data, uuid_hex = _tape(tmp_path)
    raw = bytearray(_segments(_tape_dir(data, uuid_hex))[0])
    wiped = bytearray(raw)
    assert _last_end(bytes(wiped)) > C.SEGMENT_HEADER_BYTES
    wiped[C.SEGMENT_HEADER_BYTES:] = b"\x00" * (len(wiped) - C.SEGMENT_HEADER_BYTES)   # an unused segment
    assert recovery.trim_padding(bytes(wiped)) == bytes(wiped[:C.SEGMENT_HEADER_BYTES])
    damaged = bytearray(wiped)
    damaged[C.SEGMENT_HEADER_BYTES:C.SEGMENT_HEADER_BYTES + 3] = b"\x01\x02\x03"
    assert recovery.trim_padding(bytes(damaged))[C.SEGMENT_HEADER_BYTES:C.SEGMENT_HEADER_BYTES + 3] == b"\x01\x02\x03"


# ── and the capsule still says so ───────────────────────────────────────────
def test_the_trimmed_capsule_verifies_and_lists_a_real_size(tmp_path: Path) -> None:
    capsule_path, _manifest = _capsule(tmp_path)
    report = manifest_mod.verify(cap.read_capsule(str(capsule_path)))
    assert report["ok"] is True, report
    checks = {check["check"]: check for check in report["checks"]}
    assert checks["derived_parts"]["ok"] is True
    row = cmd_ls.describe(str(capsule_path))
    assert row["readable"] is True and isinstance(row["bytes"], int)
    assert row["bytes"] < 128 * 1024, "16 MiB per capsule was the hole, not the evidence"
    assert cmd_ls._size(row["bytes"]).endswith("KiB")


@pytest.mark.parametrize("events", [1, 25])
def test_small_and_larger_runs_both_keep_every_frame(tmp_path: Path, events: int) -> None:
    data, uuid_hex = _tape(tmp_path, events=events)
    raw = _segments(_tape_dir(data, uuid_hex))[0]
    result = recovery.recover(str(data), uuid_hex)
    assert result is not None and result.capsule_path
    capsule = cap.read_capsule(result.capsule_path)
    entry = _segment_entries(capsule)[0]
    tape_frames, entry_frames = _decoded(raw), _decoded(entry)
    assert [frame["seq"] for frame in entry_frames] == [frame["seq"] for frame in tape_frames]
    assert len(entry_frames) >= events + 2               # run start + the events + run end, plus tape bookkeeping
    assert len(entry) < C.MIN_SEGMENT_BYTES // 4
