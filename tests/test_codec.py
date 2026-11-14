"""P1-01/02/04: CRC-32, canonical CBOR, ids and keyed hashes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox.codec import cbor  # noqa: E402
from blackbox.codec.crc32 import crc32  # noqa: E402
from blackbox.codec.ids import (blake2b, boot_id_hash, fingerprint, keyed_hash, new_run_id,  # noqa: E402
                                new_writer_uuid, node_id)


def test_crc_check_values():
    """RFC 1952 / gzip check value, and the format-v2 conformance vector (C4)."""
    from blackbox.codec.crc32 import CHECK_VALUE

    assert crc32(b"") == 0
    assert crc32(b"123456789") == 0xCBF43926 == CHECK_VALUE
    assert crc32(b"123456789") != 0xE3069283          # that was CRC32C, removed in v2


@given(st.binary(max_size=4096))
@settings(max_examples=200, deadline=None)
def test_crc_matches_zlib_and_is_incremental(data):
    import zlib

    assert crc32(data) == zlib.crc32(data) & 0xFFFFFFFF
    if len(data) > 3:                                  # folding in parts equals one pass
        cut = len(data) // 2
        assert crc32(data[cut:], crc32(data[:cut])) == crc32(data)


@pytest.mark.parametrize("value", [
    None, True, False, 0, 1, -1, 23, 2**32, 2**64 - 1, -2**63, "", "x", "héllo", b"", b"\x00\xff",
    [], [1, 2], {}, {"a": 1, "b": [1, {"c": None}]},
])
def test_cbor_roundtrip(value):
    assert cbor.decode_exact(cbor.encode(value)) == value


def test_cbor_is_canonical_and_order_independent():
    a = cbor.encode({"b": 1, "a": 2})
    b = cbor.encode({"a": 2, "b": 1})
    assert a == b


@given(st.dictionaries(st.text(max_size=8), st.integers(min_value=-(2 ** 63), max_value=2 ** 64 - 1),
                        max_size=6))
@settings(max_examples=100, deadline=None)
def test_cbor_dict_roundtrip(d):
    assert cbor.decode_exact(cbor.encode(d)) == d   # canonical ordering survives a round-trip


def test_cbor_rejects_trailing_and_truncated():
    blob = cbor.encode({"a": [1, 2, 3]})
    with pytest.raises(cbor.CBORError):
        cbor.decode_exact(blob + b"\x00")              # trailing bytes are never tolerated
    with pytest.raises(cbor.CBORError):
        cbor.decode_exact(blob[:-1])
    value, pos = cbor.decode(blob + b"\x00")           # the low-level API reports the consumed length
    assert pos == len(blob) and value == {"a": [1, 2, 3]}


def test_cbor_limits():
    with pytest.raises(cbor.CBORError):
        cbor.decode_exact(b"\x82" + cbor.encode(1))     # declared 2 items, only one present
    with pytest.raises(cbor.CBORError):
        cbor.encode({str(i): i for i in range(cbor.MAX_MAP_KEYS + 1)})


def test_ids_are_unique_and_fixed_width():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("r_") and len(i) == 28 for i in ids)      # 'r_' + 26 base32 chars
    assert len(new_writer_uuid()) == 16
    nid = node_id("r_x", "a/b", "command")
    assert nid.startswith("n_") and len(nid) == 22
    assert nid == node_id("r_x", "a/b", "command")                    # content-derived, stable
    assert nid != node_id("r_x", "a/b", "command", 1)


def test_hashes():
    assert len(blake2b(b"x")) == 32
    assert fingerprint({"k": 1}) == fingerprint({"k": 1})
    assert keyed_hash(b"k", b"data") != blake2b(b"data")
    assert len(boot_id_hash()) == 8


def test_keyed_hash_changes_with_key():
    assert keyed_hash(b"key1", b"same") != keyed_hash(b"key2", b"same")


def test_chain_fastpath_matches_generic():
    """The fixed-schema encoder must be byte-identical to the generic one (it runs on the publish path)."""
    from blackbox.tape import chain as chain_mod

    prev, batch = bytes(range(32)), bytes(range(32, 64))
    for first, last in ((0, 1), (1, 23), (24, 300), (1, 2 ** 40), (2 ** 63, 2 ** 64 - 1)):
        generic = chain_mod.encode_payload(prev=prev, batch=batch, first_seq=first, last_seq=last)
        buf = bytearray(256)
        n = cbor.encode_chain_into(buf, 0, prev, batch, first, last)
        assert bytes(buf[:n]) == generic
        assert cbor.decode_exact(bytes(buf[:n])) == {"last": last, "prev": prev, "batch": batch, "first": first}


def test_magic_fields_are_exactly_their_width():
    """A7: a magic shorter than its field silently shifts every following byte (it happened twice)."""
    from blackbox import constants as C

    for name, width in (("MAGIC_META", 8), ("MAGIC_HEAD", 8), ("MAGIC_SEG", 8), ("MAGIC_SEAL", 8)):
        assert len(getattr(C, name)) == width, name
    for name in ("MAGIC_FRAME", "MAGIC_FRAME_TAIL"):
        assert len(getattr(C, name)) == 4, name
    assert C.MAGIC_SEG == bytes.fromhex("4242585345473200")     # "BBXSEG2" + NUL pad, frozen in hex


def test_host_fingerprint_hashes_a_real_hostname():
    """A6: the field must not hash an empty string, and it must not need the network to be filled."""
    import platform

    from blackbox.codec.ids import host_fingerprint

    fp = host_fingerprint()
    assert len(fp) == 32
    assert fp[16:] == b"\x00" * 16
    assert fp[:16] != blake2b(b"|")[:16], "host_fingerprint must not be a constant"

    material = f"{platform.node()}|{platform.system()}|{platform.machine()}".encode()
    expected = blake2b(material)[:16]
    if platform.node():                                   # always true on the CI hosts we run on
        assert fp[:16] == expected


def test_no_socket_import_anywhere_in_the_core():
    from pathlib import Path

    hits = [p.name for p in (Path(__file__).resolve().parents[1] / "src").rglob("*.py")
            if "import socket" in p.read_text(encoding="utf-8")]
    assert not hits, f"the core must not import socket at all: {hits}"
