# ADR-004 — one checksum, CRC-32/ISO-HDLC, not CRC32C

**Status:** accepted 2026-11-14 (C4) · **Supersedes:** nothing · **Affects:** `docs/format.md` §1, §2, §3, §4, §6,
§7; plan §8.2, §8.3, §8.7, §8.8; `constants.py`, `codec/crc32.py`, every checksum field.

## Decision

Format v2 has exactly one checksum, used by the frame trailer, head slot, meta page, segment header and `.seal`:

- **CRC-32/ISO-HDLC** — polynomial `0x04C11DB7` (reflected `0xEDB88320`), init `0xFFFFFFFF`, xorout `0xFFFFFFFF`.
- Implemented as `zlib.crc32` (C, hardware-assisted where available); check value
  `crc32("123456789") == 0xCBF43926`, in the conformance set (`tests/conformance/vectors/crc32.json`).
- `meta.flags` bit 2 is reserved and must be 0; a tape that sets it is rejected with `BBX-E221`.

The pure-Python CRC32C (Castagnoli) implementation is deleted. `E3069283` no longer appears in the format.

## Why

Measured on this machine, `bench/results/2026-11-14-frame.json`, N = 100 000, warm-up 10 000:

| checksum | per KiB | consequence |
|---|---|---|
| pure-Python CRC32C, slicing-by-4 | **110.5 µs** (≈ 9 MB/s) | alone exceeds the §15.1 event budget (25 µs p50) |
| `zlib.crc32` | **0.58 µs** | 0.4 % of the budget |

The plan's P1-01 estimate of "~80 MB/s pure Python" was an estimate and was wrong by an order of magnitude; the
row is corrected in the same commit.

The CRC's job in this design is **torn/stale frame detection**: a reader must reject a half-written trailer or a
stale slot. That is a burst-error detector requirement, and CRC-32/ISO-HDLC is the standard answer — it is the
checksum of gzip, PNG and Ethernet. **Tamper evidence is not the CRC's job**; it is the BLAKE2b chain over batch
and segment bodies (`§8.12`), which no 32-bit polynomial could provide anyway.

A second algorithm would also cost every reader an implementation: the Go conformance reader (P11-15) would have
to carry both polynomials to read tapes that, in this build, do not exist.

## Consequences

- One polynomial to implement, test and document; one conformance vector.
- CRC32C tapes written by the stage-2a build (pre-2026-11-14) are **not readable** by this build. None were
  released; the format is still pre-freeze (P4-12 freezes it).
- `docs/DEVIATIONS.md` DV-005 records the change against plan §8.3.
