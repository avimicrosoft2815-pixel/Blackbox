"""Four fault kinds, and the exact code each one produces.

The row is a small table, and it exists because "the product reports a problem" is not the same as "the product reports
*this* problem": a user who pastes `BBX-E220` into the issue tracker must be able to find the paragraph that explains it,
and a support conversation that starts from the wrong code wastes the one piece of information the tool produced.

Each fault is made the way it happens — by a program writing to the tape's files — and each assertion is on the code
raised at the layer that owns the check, plus the requirement that the code is in `errors.REGISTRY` and therefore in
`docs/errors.md` (which `tools/gen_errors_doc.py` generates from it, and `tests/test_errors_doc.py` keeps in step).

| fault | made by | code | what the reader learns |
|---|---|---|---|
| corrupt meta | one payload byte of `meta` flipped | `BBX-E222` (its own checksum) / `BBX-E220` (through recovery, because the head commits that checksum) | the tape's identity block is damaged, not the frames |
| zeroed head | `head` overwritten with zeros | `BBX-E220` | neither head slot is valid: this tape cannot be adopted |
| bad CRC | one body byte flipped after the segment was sealed | `BBX-E224` | the sealed body no longer matches its seal |
| torn frame | the active segment truncated five bytes into its last frame, as a power loss leaves it | *no exception* — `recovered`/`PROCESS_DEATH`, `truncated_tail`, `truncated_bytes`, `lost_from_seq` | the recording stopped mid-write; every whole frame is kept |

The last row is the important one to state plainly: a torn frame is **normal**, not an error. A recorder that raised on
the machine being reset mid-write would fail exactly when it is needed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.errors import REGISTRY, BlackBoxError
from blackbox.rec import recovery
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import scanner, segments
from blackbox.tape import seal as seal_mod
from blackbox.tape.writer import TapeWriter


@pytest.fixture()
def tape(tmp_path: Path) -> tuple[str, str]:
    directory = str(tmp_path / "data")
    writer = TapeWriter(directory, segment_bytes=C.MIN_SEGMENT_BYTES).open()
    try:
        writer.append(C.KIND_RUN_START, cbor.encode({"run_id": "r_00000000000000fc"}), priority=0)
        for index in range(4):
            writer.append(C.KIND_MODEL_REQUEST, cbor.encode({"i": index}), priority=0, run_slot=1)
        writer.publish()
    finally:
        writer.close()
    return directory, writer.writer_uuid.hex()


def _tape_dir(directory: str, uuid: str) -> Path:
    return Path(directory) / "tape" / uuid


def test_every_code_in_this_table_is_in_the_registry_and_the_doc() -> None:
    doc = (ROOT / "docs" / "errors.md").read_text(encoding="utf-8")
    for code in ("BBX-E220", "BBX-E222", "BBX-E224"):
        assert code in REGISTRY, f"{code} is not a declared error"
        assert code in doc, f"{code} is declared but missing from docs/errors.md — run tools/gen_errors_doc.py"


def test_corrupt_meta_raises_e222_when_read_directly(tape) -> None:
    directory, uuid = tape
    path = _tape_dir(directory, uuid) / "meta"
    raw = bytearray(path.read_bytes())
    raw[40] ^= 0x01                     # a payload byte, past the format fields: the checksum must notice
    path.write_bytes(bytes(raw))
    with pytest.raises(BlackBoxError) as exc:
        meta_mod.decode(path.read_bytes()[:C.META_BYTES])
    assert exc.value.code == "BBX-E222"
    assert "checksum" in str(exc.value).lower()


def test_corrupt_meta_reaches_the_user_as_e220_through_recovery(tape) -> None:
    """The head commits the meta checksum, so a damaged meta is indistinguishable from a damaged head — and the head is
    the thing the user is told to look at. That ordering is deliberate: it is the earlier check that fires."""
    directory, uuid = tape
    path = _tape_dir(directory, uuid) / "meta"
    raw = bytearray(path.read_bytes())
    raw[40] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(BlackBoxError) as exc:
        recovery.recover(directory, uuid)
    assert exc.value.code == "BBX-E220"


def test_a_zeroed_head_raises_e220(tape) -> None:
    directory, uuid = tape
    path = _tape_dir(directory, uuid) / "head"
    path.write_bytes(b"\x00" * max(len(path.read_bytes()), C.HEAD_SLOT_BYTES * 2))
    assert head_mod.read_best(str(path)) is None
    with pytest.raises(BlackBoxError) as exc:
        recovery.recover(directory, uuid)
    assert exc.value.code == "BBX-E220"


def test_a_bad_crc_in_a_sealed_body_raises_e224(tape) -> None:
    directory, uuid = tape
    tape_dir = _tape_dir(directory, uuid)
    segment = segments.seg_path_any(str(tape_dir), segments.list_segments(str(tape_dir))[-1])
    seal_path = segments.seal_path(str(tape_dir), int(Path(segment).stem))
    assert seal_mod.verify(segment, seal_path)["frame_count"] >= 1
    with open(segment, "r+b") as handle:
        handle.seek(C.SEGMENT_HEADER_BYTES + 3)
        handle.write(b"X")
    with pytest.raises(BlackBoxError) as exc:
        seal_mod.verify(segment, seal_path)
    assert exc.value.code == "BBX-E224"


def _tear_last_frame(tape_dir: Path, lost_bytes: int = 5) -> Path:
    """Leave the tape as a power loss does: the committed head points into a frame that is only partly on disk."""
    head = head_mod.read_best(str(tape_dir / "head"))
    assert head is not None
    segment = segments.seg_path_any(str(tape_dir), segments.list_segments(str(tape_dir))[-1])
    os.truncate(segment, head.head_off - lost_bytes)
    return Path(segment)


def test_a_torn_frame_is_recorded_rather_than_raised(tape) -> None:
    directory, uuid = tape
    before = scanner.scan(directory, uuid)
    _tear_last_frame(_tape_dir(directory, uuid))
    result = recovery.recover(directory, uuid)
    assert result.run_status == "recovered"
    assert result.status_cause == "PROCESS_DEATH"
    assert result.capsule_path and Path(result.capsule_path).exists()

    from blackbox.cap import format as cap

    parsed = cap.read_capsule(result.capsule_path)
    assert parsed.root_ok, parsed.problems
    run = parsed.manifest["run"]
    assert run["status"] == "recovered"
    assert run["truncated_tail"] is True, "a torn tail must leave a mark the reader can see, not just a status word"
    assert run["lost_from_seq"] == before.last_seq


def test_the_partial_tail_is_trimmed_rather_than_copied_in(tape) -> None:
    """The scanner's job on a torn tail: keep every whole frame, report the partial one, never invent a frame.

    Before `frame.last_valid_frame_end` existed this returned **zero** frames — the backward walk from a mid-frame offset
    stops at its first step, and the whole active segment read as empty. A crash in the last 88 bytes hid the entire run;
    that is defect 94, and this assertion is its regression test.
    """
    directory, uuid = tape
    tape_dir = _tape_dir(directory, uuid)
    whole = scanner.scan(directory, uuid)
    _tear_last_frame(tape_dir, 5)
    torn = scanner.scan(directory, uuid)
    assert whole.frame_count >= 5
    assert torn.frame_count == whole.frame_count - 1, f"recovered {torn.frame_count} of {whole.frame_count} frames"
    assert torn.truncated_tail and torn.truncated_bytes >= 5
    assert torn.lost_from_seq == whole.last_seq
    assert torn.completeness() == "partial"
    assert [frame["seq"] for frame in torn.frames] == [frame["seq"] for frame in whole.frames][:-1]
