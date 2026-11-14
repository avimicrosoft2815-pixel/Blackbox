"""Conformance vectors a second-language reader must reproduce (P0-05, P11-15 input)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.codec.crc32 import CHECK_VALUE, crc32  # noqa: E402

VECTORS = ROOT / "tests" / "conformance" / "vectors" / "crc32.json"


def test_crc32_vectors():
    spec = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert spec["reflected"] is True
    for vector in spec["vectors"]:
        data = (vector["input_ascii"].encode("ascii") if "input_ascii" in vector
                else bytes.fromhex(vector["input_hex"]))
        expected = int(vector["crc32"], 16)
        assert crc32(data) == expected, vector


def test_spec_claims_the_same_check_value_as_the_code():
    spec = json.loads(VECTORS.read_text(encoding="utf-8"))
    check = next(v for v in spec["vectors"] if v.get("input_ascii") == "123456789")
    assert int(check["crc32"], 16) == CHECK_VALUE
