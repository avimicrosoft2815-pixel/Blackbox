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


def batch_hash(frames: list[bytes]) -> bytes:
    h = blake2b(b"".join(frames))
    return h


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
    return d


class ChainState:
    """Tracks the running chain value for one writer."""

    __slots__ = ("value",)

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
