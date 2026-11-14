"""Canonical CBOR subset (P1-02/P1-03) — maps, arrays, ints, bytes, text, bool, null, float64.

Encoder rules (docs/format.md §7): shortest-form integers, definite lengths only, map keys sorted by
their encoded bytes, no tags, no indefinite items. Decoder rejects everything outside the subset with
`CBORError`, so a hostile payload can never reach a Python object graph we did not choose.
"""
from __future__ import annotations

import struct

from blackbox.errors import bbx

MAX_DEPTH = 32
MAX_MAP_KEYS = 4096
MAX_BYTES = 1 << 20
MAX_INT = 1 << 64


class CBORError(ValueError):
    pass


# ── encoding ────────────────────────────────────────────────────────────────
def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 0x100:
        return bytes([(major << 5) | 24, value])
    if value < 0x10000:
        return bytes([(major << 5) | 25]) + struct.pack(">H", value)
    if value < 0x100000000:
        return bytes([(major << 5) | 26]) + struct.pack(">I", value)
    if value < MAX_INT:
        return bytes([(major << 5) | 27]) + struct.pack(">Q", value)
    raise CBORError("integer too large for the subset")


def _int(n: int) -> bytes:
    if n < 0:
        return _head(1, -1 - n)
    return _head(0, n)


def encode(obj, _depth: int = 0) -> bytes:
    """Canonical encode. Type dispatch on `type(obj)` with a small-int fast path: the hot path avoids an
    isinstance chain and a function call per integer.

    A bytearray-at-offset encoder was implemented and measured (P1-17) and came out *slower* in CPython
    (~16 us vs ~11 us for a 9-key event payload): the extra `_grow`/`_head` calls cost more than the
    intermediate bytes they avoid. The measurement is in `bench/results/2026-11-14-frame.json`.
    """
    if _depth > MAX_DEPTH:
        raise CBORError("nesting too deep")
    t = type(obj)
    if t is int:
        if 0 <= obj < 24:
            return _SMALL[obj]
        return _int(obj)
    if t is str:
        b = obj.encode("utf-8")
        return _head(3, len(b)) + b
    if t is bytes or t is bytearray or t is memoryview:
        b = obj if t is bytes else bytes(obj)
        return _head(2, len(b)) + b
    if t is bool:
        return _TRUE if obj else _FALSE
    if obj is None:
        return _NULL
    if t is dict:
        if len(obj) > MAX_MAP_KEYS:
            raise CBORError("map too large")
        items = [(_enc_key(k, _depth), encode(v, _depth + 1)) for k, v in obj.items()]
        items.sort(key=_first)
        return _head(5, len(items)) + b"".join([k + v for k, v in items])
    if t is list or t is tuple:
        return _head(4, len(obj)) + b"".join([encode(v, _depth + 1) for v in obj])
    if t is float:
        return b"\xfb" + struct.pack(">d", obj)
    raise CBORError(f"cannot encode {type(obj).__name__}")


def _first(pair):
    return pair[0]


def _enc_key(key, depth: int) -> bytes:
    # map keys are almost always short text: keep them out of the general encoder's dispatch cost
    if type(key) is str and key.isascii() and len(key) < 24:
        b = key.encode("ascii")
        return _SHORT_TEXT[len(b)] + b if len(b) < 24 else _head(3, len(b)) + b
    return encode(key, depth + 1)


_SMALL = tuple(bytes((i,)) for i in range(24))
_TRUE, _FALSE, _NULL = b"\xf5", b"\xf4", b"\xf6"


def _short_text_table() -> tuple[bytes, ...]:
    return tuple(bytes((0x60 | n,)) for n in range(24))


_SHORT_TEXT = _short_text_table()


# ── fixed-schema encoders: precomputed key bytes, no intermediate objects ────
_KEY_PREV, _KEY_BATCH, _KEY_FIRST, _KEY_LAST = b"\x64prev", b"\x65batch", b"\x65first", b"\x64last"
assert (len(_KEY_PREV), len(_KEY_BATCH), len(_KEY_FIRST), len(_KEY_LAST)) == (5, 6, 6, 5)


def encode_chain_into(buf: bytearray, off: int, prev: bytes, batch: bytes, first_seq: int,
                      last_seq: int) -> int:
    """Encode the CHAIN payload straight into `buf` (writer publish path).

    Canonical key order for this schema is `last, prev, batch, first` (sorted by encoded key bytes:
    0x64 "last" < 0x64 "prev" < 0x65 "batch" < 0x65 "first"), verified byte-for-byte against the generic
    encoder in `tests/test_codec.py::test_chain_fastpath_matches_generic`.
    """
    pos = off
    buf[pos] = 0xA4                       # map(4)
    pos += 1

    buf[pos:pos + 5] = _KEY_LAST          # 0x64 "last"
    pos += 5
    if 0 <= last_seq < 24:
        buf[pos] = last_seq
        pos += 1
    else:
        enc = _int(last_seq)
        buf[pos:pos + len(enc)] = enc
        pos += len(enc)

    for key, blob in ((_KEY_PREV, prev), (_KEY_BATCH, batch)):
        n = len(key)
        buf[pos:pos + n] = key
        pos += n
        buf[pos] = 0x58                   # bytes(32)
        buf[pos + 1] = 32
        pos += 2
        buf[pos:pos + 32] = blob
        pos += 32

    buf[pos:pos + 6] = _KEY_FIRST         # 0x65 "first"
    pos += 6
    if 0 <= first_seq < 24:
        buf[pos] = first_seq
        pos += 1
    else:
        enc = _int(first_seq)
        buf[pos:pos + len(enc)] = enc
        pos += len(enc)
    return pos - off




# ── decoding ────────────────────────────────────────────────────────────────
def _read_len(buf: bytes, pos: int, info: int) -> tuple[int, int]:
    if info < 24:
        return info, pos
    if info == 24:
        return buf[pos], pos + 1
    if info == 25:
        return struct.unpack_from(">H", buf, pos)[0], pos + 2
    if info == 26:
        return struct.unpack_from(">I", buf, pos)[0], pos + 4
    if info == 27:
        return struct.unpack_from(">Q", buf, pos)[0], pos + 8
    if info == 31:
        raise CBORError("indefinite length items are not allowed")
    raise CBORError("reserved additional-information value")


def decode(buf: bytes, pos: int = 0, allow_trailing: bool = False, _depth: int = 0):
    """Decode one item at `pos`; returns (value, next_pos)."""
    if _depth > MAX_DEPTH:
        raise CBORError("nesting too deep")
    if pos >= len(buf):
        raise CBORError("unexpected end of input")
    initial = buf[pos]
    pos += 1
    major, info = initial >> 5, initial & 0x1F

    if major == 0:
        v, pos = _read_len(buf, pos, info)
        return v, pos
    if major == 1:
        v, pos = _read_len(buf, pos, info)
        return -1 - v, pos
    if major == 2:
        n, pos = _read_len(buf, pos, info)
        if n > MAX_BYTES:
            raise CBORError("byte string too large")
        if pos + n > len(buf):
            raise CBORError("truncated byte string")
        return bytes(buf[pos:pos + n]), pos + n
    if major == 3:
        n, pos = _read_len(buf, pos, info)
        if n > MAX_BYTES:
            raise CBORError("text string too large")
        if pos + n > len(buf):
            raise CBORError("truncated text string")
        try:
            return buf[pos:pos + n].decode("utf-8"), pos + n
        except UnicodeDecodeError as exc:
            raise CBORError("invalid utf-8 in text string") from exc
    if major == 4:
        n, pos = _read_len(buf, pos, info)
        if n > MAX_MAP_KEYS:
            raise CBORError("array too large")
        out = []
        for _ in range(n):
            v, pos = decode(buf, pos, _depth=_depth + 1)
            out.append(v)
        return out, pos
    if major == 5:
        n, pos = _read_len(buf, pos, info)
        if n > MAX_MAP_KEYS:
            raise CBORError("map too large")
        out = {}
        for _ in range(n):
            k, pos = decode(buf, pos, _depth=_depth + 1)
            v, pos = decode(buf, pos, _depth=_depth + 1)
            if not isinstance(k, (str, bytes, int)):
                raise CBORError("unsupported map key type")
            out[k] = v
        return out, pos
    if major == 6:
        raise CBORError("tags are not allowed")
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info == 22:
            return None, pos
        if info == 27:
            return struct.unpack_from(">d", buf, pos)[0], pos + 8
        raise CBORError("unsupported simple/float value")
    raise CBORError("unreachable")


def decode_exact(buf: bytes):
    """Decode one item and reject trailing bytes (used for frame payloads)."""
    value, pos = decode(buf, 0)
    if pos != len(buf):
        raise CBORError("trailing bytes after CBOR item")
    return value


def decode_or_raise(buf: bytes):
    """decode_exact with the registry code attached (BBX-E151)."""
    try:
        return decode_exact(buf)
    except CBORError as exc:
        raise bbx("BBX-E151", str(exc)) from exc
