# BlackBox on-disk formats — v2 (segment tape)

**Status:** frozen for v2 by this document (plan §8, 1.1). A reader implemented from this file alone must pass
`tests/conformance/` (P0-05) — including a non-Python reader later (P11-15).
All integers are little-endian. All CRCs are **CRC32C (Castagnoli, reflected, init 0xFFFFFFFF, xorout 0xFFFFFFFF)**.
Hashes are **BLAKE2b-256** unless stated. Reserved bytes are zero and MUST be ignored by readers.

## 1. Directory layout (one directory per writer)

```
<data_dir>/tape/<writer_uuid>/meta           4096 B  immutable identity
<data_dir>/tape/<writer_uuid>/head            512 B  two 64 B commit slots
<data_dir>/tape/<writer_uuid>/lock           ≤ 512 B writer lease (JSON, see §6)
<data_dir>/tape/<writer_uuid>/00000001.seg           sealed segment (has 00000001.seal)
<data_dir>/tape/<writer_uuid>/00000001.seal   256 B  footer sidecar, written once
<data_dir>/tape/<writer_uuid>/00000002.seg           ACTIVE segment (preallocated, mmap'd)
```

`writer_uuid` is 16 random bytes, lower-case hex in filenames. Segment numbers are decimal, zero-padded to 8.

## 2. `meta` (4096 B, written once, never rewritten)

| Offset | Size | Field |
|---|---|---|
| 0x000 | 8 | magic `BBXTAPE2` (0x32 0x45 0x50 0x41 0x54 0x58 0x42 0x42) |
| 0x008 | 2 | format_version = 2 |
| 0x00A | 2 | flags: bit0 = layout (0 segments, 1 ring); bits1..15 reserved |
| 0x00C | 4 | segment_bytes (u32) |
| 0x010 | 16 | writer_uuid |
| 0x020 | 8 | created_wall_ns (i64, `time.time_ns()`) |
| 0x028 | 8 | created_mono_ns (i64, `time.perf_counter_ns()`) |
| 0x030 | 8 | boot_id_hash (first 8 B of BLAKE2b-256 of the boot id string; zeros if unavailable) |
| 0x038 | 4 | pid (u32) |
| 0x03C | 4 | vantage (u32): 1 sdk, 2 proxy, 3 hook-inbox, 4 recovery |
| 0x040 | 8 | proc_start_ns (u64; 0 if unavailable) |
| 0x048 | 32 | host_fingerprint (BLAKE2b-128 over hostname+os+machine, zero-padded to 32) |
| 0x068 | 8 | attrs_json_ref (seq of an `INTERN` frame; 0 = none) |
| 0x070 | 2 | schema_ver (u16) = 1 |
| 0x072 | 0x0F7E | reserved |
| 0x0FF0 | 4 | meta_crc32c over bytes [0x000, 0x0FF0) |
| 0x0FF4 | 12 | reserved |

Reader rule: reject with `BBX-E222` if the CRC fails; `BBX-E221` if `format_version` is greater than the reader's maximum (2).

## 3. `head` (512 B; two 64 B slots at 0x000 and 0x040)

Each slot is written in full, then flushed; slots alternate so a torn write can never destroy the newest valid record.

| Offset in slot | Size | Field |
|---|---|---|
| 0x00 | 8 | magic `BBXHEAD2` |
| 0x08 | 8 | gen (u64; strictly increasing) |
| 0x10 | 8 | head_seq (u64; last committed seq) |
| 0x18 | 8 | head_off (u64; write cursor inside the active segment, ≥ 4096) |
| 0x20 | 8 | tail_seq (u64; lowest still-retained seq) |
| 0x28 | 8 | sealed_seq (u64; hint only — `.seal` files are authoritative) |
| 0x30 | 4 | active_segment (u32, 1-based) |
| 0x34 | 4 | flags: bit0 DEGRADED_NO_SPACE, bit1 DEGRADED_CRC, bit2 KEEPER_DOWN, bit3 ROLL_PENDING |
| 0x38 | 4 | dropped_events (u32, saturating) |
| 0x3C | 4 | head_crc32c over slot bytes [0x00, 0x3C) |

Reader rule: take the slot with the **greatest valid `gen`**; if both CRCs fail, fall back to a forward resync scan.

## 4. Frames (the event unit)

Frame = 48 B header ‖ payload ‖ 8 B trailer. `frame_len = 48 + payload_len + 8`, multiple of 8 (payloads are zero-padded).

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `BBXF` (0x46 0x58 0x42 0x42) |
| 4 | 4 | frame_len (u32) |
| 8 | 8 | seq (u64) |
| 16 | 8 | wall_ns (i64) |
| 24 | 8 | mono_ns (i64) |
| 32 | 2 | kind (u16) |
| 34 | 2 | flags: bit0 has_blob, bit1 truncated, bit2 redaction_failed, bit3 wrap_pad, bit4..7 priority |
| 36 | 2 | run_slot (u16) |
| 38 | 2 | node_slot (u16) |
| 40 | 4 | payload_len (u32) |
| 44 | 4 | crc32c over header[0,44) ‖ payload ‖ trailer |
| 48 | payload_len | payload (canonical CBOR, see §7) |
| 48+payload_len | 4 | tail magic `BBXT` |
| 52+payload_len | 4 | tail_len (= frame_len) |

**Write order inside a frame:** payload+padding → trailer → header. A crash mid-frame therefore leaves no parseable
header, and the backward scanner can never mistake a partial frame for committed data.
**Trailer** makes the backward walk O(1): `prev_off = off - tail_len`.

Kind registry (append-only): `1 SEG_END`, `2 CHAIN`, `3 INTERN`, `4 GAP`, `5 DEGRADED`, `6 HEARTBEAT`,
`16 NODE_OPEN`, `17 NODE_CLOSE`, `18 EDGE`, `32 MODEL_REQUEST`, `33 MODEL_RESPONSE`, `34 MODEL_CHUNK`,
`35 MODEL_ERROR`, `48 TOOL_REQUEST`, `49 TOOL_RESULT`, `80 RUN_START`, `81 RUN_END`, `96 EXCEPTION`, `112 MARK`.

## 5. Segment file `<n>.seg` = 4096 B header ‖ body of frames

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | magic `BBXSEG2` + NUL pad to 8 bytes |
| 8 | 2 | version = 2 |
| 10 | 2 | flags (reserved) |
| 12 | 16 | writer_uuid |
| 28 | 4 | segment_no (u32) |
| 32 | 8 | start_seq (u64; first frame's seq; 0 when empty) |
| 40 | 4 | segment_bytes (u32) |
| 44 | 4 | codec: 0 raw, 1 zlib, 2 zstd (only ≠ 0 after compaction) |
| 48 | 8 | created_wall_ns (i64) |
| 56 | 8 | created_mono_ns (i64) |
| 64 | 32 | prev_chain (chain value at the end of segment n−1; zeros for the first) |
| 96 | 4 | header_crc32c over bytes [0, 96) |
| 100 | 3996 | reserved |
| 4096 | body | frames, byte-identical to what the writer committed; unused tail is zero |

Segments are **fully preallocated** at creation (`posix_fallocate`, else zero-fill) and never sparse:
a page fault into a hole under `ENOSPC`/EOF raises `SIGBUS` in the faulting process (S17 proves this).

## 6. `.seal` sidecar (256 B, written once via tmp+fsync+rename)

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | magic `BBXSEAL1` |
| 8 | 4 | segment_no |
| 12 | 4 | reason: 1 ROLLED, 2 RUN_END, 3 RECOVERED, 4 SHUTDOWN |
| 16 | 8 | start_seq |
| 24 | 8 | end_seq |
| 32 | 8 | frame_count |
| 40 | 8 | body_len (bytes of valid frames after the 4096 B header) |
| 48 | 4 | body_crc32c over the body bytes `[4096, 4096+body_len)` |
| 52 | 32 | body_hash (BLAKE2b-256 of the same body bytes) |
| 84 | 32 | chain = BLAKE2b-256(prev_chain ‖ body_hash) |
| 116 | 8 | closed_wall_ns (i64) |
| 124 | 16 | sealer (pid u32 ‖ proc_start_ns u64 ‖ 4 reserved) |
| 140 | 112 | reserved |
| 252 | 4 | seal_crc32c over bytes [0, 252) |

A segment **with** a valid `.seal` is immutable. A segment **without** one is active-or-dead and is read by the
backward CRC walk; the recoverer writes the missing `.seal` with `reason = RECOVERED`.

## 7. Payload encoding (canonical CBOR subset)

Encoder MUST emit: shortest-form integers, definite lengths only, map keys sorted by their encoded bytes,
UTF-8 text (invalid sequences are base64ed under a `~b64` key), and no tags/indefinite items/floats unless
explicitly allowed. Decoder MUST reject: nesting > 32, maps > 4096 keys, tags, indefinite items, trailing bytes.
Violations raise `CBORError` (`BBX-E151`).

## 8. Writer lease `lock` (JSON, ≤ 512 B)

```json
{"v":1,"pid":1234,"proc_start_ns":0,"boot_id_hash":"hex16","nonce":"hex16","created_ns":0}
```
Acquired with `O_CREAT|O_EXCL`. Stale iff the boot id differs, or the pid is gone, or the pid's process start
time differs (PID reuse), or the file is older than 24 h with no frame progress. Never steal a live lease.

## 9. Conformance vectors

`tests/conformance/vectors/*.bin` + `index.json`: a meta page, a head page, a 3-frame segment, a `.seal`, and the
expected decoded dicts. `tests/conformance/test_conformance.py` decodes them with an independent parser written
from this document.
