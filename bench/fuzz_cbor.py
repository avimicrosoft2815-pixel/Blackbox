"""P1-03 — CBOR decoder fuzz: 100 000 buffers, and nothing but `CBORError` may escape.

The decoder is the only code in the project that parses untrusted bytes with a loop, so the claim "a random
buffer never raises anything else" is tested rather than asserted. Inputs are three kinds: pure random bytes,
random bytes with valid-looking CBOR headers spliced in (depth, map size, indefinite markers, tags), and
mutations of one real encoded event — the third kind is what finds bounds bugs in a decoder that is correct on
well-formed input.

Usage: PYTHONPATH=src python bench/fuzz_cbor.py [--iterations 100000] [--seed 20261114] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.codec import cbor

EVENT = {"p": "openai", "m": "gpt-4o-mini", "pt": 3, "ph": b"\x01" * 32, "st": 1, "te": 2,
         "pm": {"temperature": 0.2, "max_tokens": 256}}


def make_input(rng: random.Random, seed_bytes: bytes) -> bytes:
    kind = rng.randrange(3)
    if kind == 0:
        return os.urandom(rng.randrange(0, 64))
    if kind == 1:
        head = bytes([rng.choice([0x58, 0x59, 0x5A, 0x5B, 0x78, 0x79, 0x7A, 0x7B, 0x98, 0x99, 0x9A,
                                  0x9B, 0xB8, 0xB9, 0xBA, 0xBB, 0xD8, 0xD9, 0xDA, 0xDB, 0x9F, 0xBF, 0xFF])])
        return head + os.urandom(rng.randrange(0, 32))
    buf = bytearray(seed_bytes)
    for _ in range(rng.randrange(1, 4)):
        pos = rng.randrange(len(buf))
        buf[pos] = rng.randrange(256)
    return bytes(buf[: rng.randrange(1, len(buf) + 1)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    seed_bytes = cbor.encode(EVENT)
    ok = rejected = 0
    failures: list[dict] = []
    started = time.perf_counter()
    for i in range(args.iterations):
        data = make_input(rng, seed_bytes)
        try:
            cbor.decode_exact(data)
            ok += 1
        except cbor.CBORError:
            rejected += 1
        except Exception as exc:
            failures.append({"iteration": i, "input_hex": data.hex()[:200],
                             "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) >= 10:
                break
    elapsed = time.perf_counter() - started
    out = {"date": time.strftime("%Y-%m-%d"), "probe": "P1-03 CBOR decoder fuzz",
           "iterations": args.iterations, "seed": args.seed, "decoded": ok, "rejected": rejected,
           "non_cbor_exceptions": failures, "elapsed_s": round(elapsed, 1),
           "inputs_per_s": round(args.iterations / elapsed, 1) if elapsed else None,
           "claim": "100k random buffers raise nothing but CBORError"}
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-fuzz-cbor.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}: {ok} decoded, {rejected} rejected, {len(failures)} non-CBOR exceptions")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
