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


def encode(obj, _depth: int = 0) -> bytes:
    if _depth > MAX_DEPTH:
        raise CBORError("nesting too deep")
    if obj is None:
        return b"\xf6"
    if obj is True:
        return b"\xf5"
    if obj is False:
        return b"\xf4"
    if isinstance(obj, int):
        if obj >= 0:
            return _head(0, obj)
        return _head(1, -1 - obj)
    if isinstance(obj, float):
        return b"\xfb" + struct.pack(">d", obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        b = bytes(obj)
        return _head(2, len(b)) + b
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return _head(3, len(b)) + b
    if isinstance(obj, (list, tuple)):
        return _head(4, len(obj)) + b"".join(encode(v, _depth + 1) for v in obj)
    if isinstance(obj, dict):
        if len(obj) > MAX_MAP_KEYS:
            raise CBORError("map too large")
        items = [(encode(k, _depth + 1), encode(v, _depth + 1)) for k, v in obj.items()]
        items.sort(key=lambda kv: kv[0])          # canonical: sort by encoded key bytes
        return _head(5, len(items)) + b"".join(k + v for k, v in items)
    raise CBORError(f"cannot encode {type(obj).__name__}")


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
