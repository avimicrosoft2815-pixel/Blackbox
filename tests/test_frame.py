"""P1-01: frame layout, trailer validation, and the backward walk."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox import constants as C
from blackbox.tape import frame as fm


def test_header_is_48_bytes():
    assert C.FRAME_HEADER_BYTES == 48
    assert C.FRAME_OVERHEAD_BYTES == 56


def test_roundtrip():
    blob = fm.encode(7, C.KIND_MODEL_REQUEST, b"payload" * 3, priority=0, run_slot=3, node_slot=2)
    fields, nxt = fm.decode(blob, 0)
    assert fields["seq"] == 7
    assert fields["kind"] == C.KIND_MODEL_REQUEST
    assert fields["payload"] == b"payload" * 3
    assert nxt == len(blob) == fm.frame_size(len(fields["payload"]))


@given(st.binary(max_size=3000), st.integers(min_value=0, max_value=3))
@settings(max_examples=150, deadline=None)
def test_padding_and_alignment(payload, priority):
    blob = fm.encode(1, 2, payload, priority=priority)
    assert len(blob) % 8 == 0
    assert len(blob) == fm.frame_size(len(payload))
    fields, nxt = fm.decode(blob, 0)
    assert fields["payload"] == payload
    assert nxt == len(blob)


def test_crc_covers_the_documented_scope():
    """The CRC must cover header[0,44) + payload + trailer, and nothing else (C4, one algorithm)."""
    from blackbox.codec.crc32 import checksum_parts

    blob = fm.encode(1, 2, b"abc")
    stored = int.from_bytes(blob[44:48], "little")
    assert stored == checksum_parts((blob[:44], blob[48:]))


def test_single_bit_flip_is_detected():
    blob = bytearray(fm.encode(1, 2, b"abcdef"))
    for i in (10, 30, len(blob) - 1):
        bad = bytearray(blob)
        bad[i] ^= 0x01
        with pytest.raises(fm.FrameError):
            fm.decode(bytes(bad), 0)


def test_backward_scan_finds_consecutive_frames():
    blobs = [fm.encode(i + 1, 2, b"x" * (i * 3)) for i in range(6)]
    buf = b"".join(blobs)
    found = fm.scan_backwards(buf, len(buf), min_offset=0)
    assert [f["seq"] for f in found] == [1, 2, 3, 4, 5, 6]        # returned in tape order
    assert len(found) == 6
    last = found[-1]
    assert last["offset"] + last["len"] == len(buf)
    boundary = buf.find(blobs[2])
    assert [f["seq"] for f in fm.scan_backwards(buf, boundary, min_offset=0)] == [1, 2]
    # a cursor that is not a frame boundary yields nothing: the walk refuses to guess (recovery needs a
    # forward resync scan instead, plan §14.3)
    assert fm.scan_backwards(buf, boundary + 5, min_offset=0) == []
