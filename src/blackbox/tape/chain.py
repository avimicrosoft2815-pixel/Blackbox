"""Batch `CHAIN` frames and per-segment chain roots (P1-18, plan §8.12).

Level 1 (batch): every publish appends a `CHAIN` frame holding `{prev, batch_hash, first_seq, last_seq}`.
Level 2 (segment): the `.seal` footer stores `chain = BLAKE2b(prev_chain ‖ body_hash)`; the next segment's
header carries `prev_chain`. Level 3 (capsule) is P4.

This is tamper-evidence for accidental corruption and careless edits. It is **not** protection against an
attacker who can rewrite the whole directory and recompute the chain; the keyed variant narrows that.
"""
from __future__ import annotations

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.codec.ids import blake2b

ZERO = b"\x00" * 32

# upper bound on one CHAIN frame: two 32-byte hashes + three u64s, plus the 56-byte frame overhead, padded
MAX_CHAIN_PAYLOAD_BYTES = 32 + 32 + 3 * 9 + 8
MAX_CHAIN_FRAME_BYTES = 56 + MAX_CHAIN_PAYLOAD_BYTES + 8


def batch_hash(frames: list[bytes]) -> bytes:
    h = blake2b(b"".join(frames))
    return h


def batch_hash_spans(mm, spans) -> bytes:
    """Batch hash straight off the mapped segment: no frame bytes are ever concatenated in memory."""
    import hashlib

    h = hashlib.blake2b(digest_size=32)
    view = memoryview(mm)
    for off, size in spans:
        h.update(view[off:off + size])
    return h.digest()


def chain_value(prev: bytes, body_hash: bytes) -> bytes:
    return blake2b(prev + body_hash)


def encode_payload(*, prev: bytes, batch: bytes, first_seq: int, last_seq: int) -> bytes:
    return cbor.encode({"prev": prev, "batch": batch, "first": first_seq, "last": last_seq})


def decode_payload(payload: bytes) -> dict:
    d = cbor.decode_or_raise(payload)
    for key in ("prev", "batch"):
        if not isinstance(d.get(key), (bytes, type(None))) or len(d[key] or b"") != 32:
            raise cbor.CBORError("chain payload: bad hash field")
    if not isinstance(d["first"], int) or not isinstance(d["last"], int):
        raise cbor.CBORError("chain payload: bad seq range")
    return dict(d)


class ChainState:
    """Tracks the running chain value for one writer."""

    __slots__ = ("value")

    def __init__(self, value: bytes = ZERO):
        if len(value) != 32:
            raise ValueError("chain value must be 32 bytes")
        self.value = value

    def extend(self, body_hash: bytes) -> bytes:
        self.value = chain_value(self.value, body_hash)
        return self.value

    @property
    def kind(self) -> int:
        return C.KIND_CHAIN
