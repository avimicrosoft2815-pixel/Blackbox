# BlackBox on-disk formats — v2 (segment tape)

**Status:** frozen for v2 by this document (plan §8, 1.1). A reader implemented from this file alone must pass
`tests/conformance/` (P0-05) — including a non-Python reader later (P11-15).
All integers are little-endian.

**Checksum — one algorithm in format v2.** Every 4-byte checksum field (frame, head slot, meta page, segment
header, `.seal`) is **CRC-32/ISO-HDLC**: polynomial `0x04C11DB7` (reflected `0xEDB88320`), init `0xFFFFFFFF`,
xorout `0xFFFFFFFF`. Check value: `crc32("123456789") == 0xCBF43926`. Fields keep short historical names
(`crc32`), and `meta.flags` bit 2 is **reserved and must be 0**; a reader that sees it set rejects the tape with
`BBX-E221`.

Why one algorithm and why this one: a CRC here detects torn or stale frames — tamper evidence is the BLAKE2b
chain (`§8.12`), not the CRC. Pure-Python CRC32C measured 110.5 µs per KiB (≈ 9 MB/s) against 0.58 µs per KiB
for `zlib.crc32` (0.58 µs per KiB measured), which alone would have broken the `§15.1` event budget, and a second algorithm would force
every reader (including the Go one) to implement it for nothing. Recorded as `DV-005` and
`docs/adr/ADR-004-crc32-not-crc32c.md`.

**Frozen magic values (exact bytes).**

| name | bytes | hex |
|---|---|---|
| `meta` | `BBXTAPE2` | `42 42 58 54 41 50 45 32` |
| `head` | `BBXHEAD2` | `42 42 58 48 45 41 44 32` |
| segment | `BBXSEG2` + `0x00` | `42 42 58 53 45 47 32 00` |
| seal | `BBXSEAL1` | `42 42 58 53 45 41 4C 31` |
| frame | `BBXF` | `42 42 58 46` |
| frame trailer | `BBXT` | `42 42 58 54` |
Hashes are **BLAKE2b-256** unless stated. Reserved bytes are zero and MUST be ignored by readers.

## The commit point is the head publish, not the store

Frames reach the active segment with one `pwrite` per group-commit batch (ADR-005) and the commit is **one
`pwrite` of the 64 B head slot** (C21); nothing in `src/` is mapped, and nothing is sparse-then-mapped.
Nothing about a byte of this format changed when that decision was taken: a reader still trusts only frames
below the published `head_off`, the head is still the two-slot CRC-checked record that commits a batch, and the
`.seal` footer still covers the same byte range. What changed is that the writer can now report `ENOSPC` and
degrade instead of taking a page fault, and that `mode=process` costs **two syscalls per batch** (segment data, head slot) instead of none.

## 1. Directory layout (one directory per writer)

```
<data_dir>/tape/<writer_uuid>/meta           4096 B  immutable identity
<data_dir>/tape/<writer_uuid>/head            512 B  two 64 B commit slots
<data_dir>/tape/<writer_uuid>/lock           ≤ 512 B writer lease (JSON, see §6)
<data_dir>/tape/<writer_uuid>/00000001.seg           sealed segment (has 00000001.seal)
<data_dir>/tape/<writer_uuid>/00000001.seal   256 B  footer sidecar, written once
<data_dir>/tape/<writer_uuid>/00000002.seg           ACTIVE segment (preallocated, written by pwrite)
```

`writer_uuid` is 16 random bytes, lower-case hex in filenames. Segment numbers are decimal, zero-padded to 8.

## 2. `meta` (4096 B, written once, never rewritten)

| Offset | Size | Field |
|---|---|---|
| 0x000 | 8 | magic `BBXTAPE2` (`42 42 58 54 41 50 45 32`) |
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
| 0x0FF0 | 4 | crc32 over bytes [0x000, 0x0FF0) |
| 0x0FF4 | 12 | reserved |

Reader rule: reject with `BBX-E222` if the CRC fails; `BBX-E221` if `format_version` is greater than the reader's maximum (2).

## 3. `head` (512 B; two 64 B slots at 0x000 and 0x040)

Each slot is written in full, then flushed; slots alternate so a torn write can never destroy the newest valid record.

| Offset in slot | Size | Field |
|---|---|---|
| 0x00 | 8 | magic `BBXHEAD2` (`42 42 58 48 45 41 44 32`) |
| 0x08 | 8 | gen (u64; strictly increasing) |
| 0x10 | 8 | head_seq (u64; last committed seq) |
| 0x18 | 8 | head_off (u64; write cursor inside the active segment, ≥ 4096) |
| 0x20 | 8 | tail_seq (u64; lowest still-retained seq) |
| 0x28 | 8 | sealed_seq (u64; hint only — `.seal` files are authoritative) |
| 0x30 | 4 | active_segment (u32, 1-based) |
| 0x34 | 4 | flags: bit0 DEGRADED_NO_SPACE, bit1 DEGRADED_CRC, bit2 KEEPER_DOWN, bit3 ROLL_PENDING |
| 0x38 | 4 | dropped_events (u32, saturating) |
| 0x3C | 4 | head checksum (profile) over slot bytes [0x00, 0x3C) |

Reader rule: take the slot with the **greatest valid `gen`**; if both CRCs fail, fall back to a forward resync scan.

## 4. Frames (the event unit)

Frame = 48 B header ‖ payload ‖ 8 B trailer. `frame_len = 48 + payload_len + 8`, multiple of 8 (payloads are zero-padded).

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `BBXF` (`42 42 58 46`) |
| 4 | 4 | frame_len (u32) |
| 8 | 8 | seq (u64) |
| 16 | 8 | wall_ns (i64) |
| 24 | 8 | mono_ns (i64) |
| 32 | 2 | kind (u16) |
| 34 | 2 | flags: bit0 has_blob, bit1 truncated, bit2 redaction_failed, bit3 wrap_pad, bit4..7 priority |
| 36 | 2 | run_slot (u16) |
| 38 | 2 | node_slot (u16) |
| 40 | 4 | payload_len (u32) |
| 44 | 4 | crc32 over header[0,44) ‖ payload ‖ trailer |
| 48 | payload_len | payload (canonical CBOR, see §7) |
| 48+payload_len | 4 | tail magic `BBXT` |
| 52+payload_len | 4 | tail_len (= frame_len) |

**Write order inside a frame:** payload+padding → trailer → header. A crash mid-frame therefore leaves no parseable
header, and the backward scanner can never mistake a partial frame for committed data.
**Trailer** makes the backward walk O(1): `prev_off = off - tail_len`.

Kind registry (append-only): `1 SEG_END`, `2 CHAIN`, `3 INTERN`, `4 GAP`, `5 DEGRADED`, `6 HEARTBEAT`,
`16 NODE_OPEN`, `17 NODE_CLOSE`, `18 EDGE`, `32 MODEL_REQUEST`, `33 MODEL_RESPONSE`, `34 MODEL_CHUNK`,
`35 MODEL_ERROR`, `48 TOOL_REQUEST`, `49 TOOL_RESULT`, `80 RUN_START`, `81 RUN_END`, `96 EXCEPTION`, `112 MARK`.

### 4.1 `GAP` frames — a declared hole, not a missing frame

`GAP` (kind 4) is the only frame that is allowed to *shorten* the readable tape. Payload (canonical CBOR):
`{"from": u64, "to": u64, "reason": ascii, "segments": [u32…], "prev": 32-byte chain value}`.

* `from`/`to` are seq numbers, inclusive, and cover the **whole** pruned range including each deleted segment's
  `SEG_END` frame (that frame dies with its segment; a `GAP` that stopped one seq short would be a hole nobody
  announced).
* `reason` is `RETENTION` today; the reader treats any committed `GAP` as a declaration and any seq that no
  `GAP` covers as corruption (`MISSING`).
* `prev` is the writer's chain value at the moment the `GAP` was committed, which makes the hole a *position in
  the chain* rather than a break: `tape.scanner.verify_chain` walks the per-segment links (each segment header's
  `prev_chain`, then every `CHAIN` frame's `prev`/`batch`) and requires every declared hole's `prev` to be a value
  that walk passes through. A `GAP` pointing anywhere else is reported as a problem, not accepted.
* Order is fixed: **commit `GAP` → publish → unlink `.seg` → unlink `.seal` → `fsync(dir)`**. A kill in the
  middle leaves either a declared hole whose files still exist (harmless) or a deleted file whose `GAP` is
  already committed (correct). It can never leave a deleted file with no declaration.
* A segment that holds a `GAP` frame is a **tombstone segment** and is never pruned while it is the only copy of
  the declaration. When retention deletes a contiguous pruned prefix, the new `GAP` covers the merged span and
  the older tombstone segments are removed in the same pass, so tombstones do not accumulate.

Scanner view: `ScanResult.gaps` lists the holes a reader sees, each `{"from", "to", "reason"}` with `reason` taken
from the covering `GAP` (`RETENTION`) or `MISSING` when none covers it; `ScanResult.gaps_declared` lists the
`GAP` frames themselves; `ScanResult.gaps_unreadable` records a `GAP` that does not decode — an unreadable
declaration is *not* a licence to call a hole declared.

## 5. Segment file `<n>.seg` = 4096 B header ‖ body of frames

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | magic `BBXSEG2` + `0x00` (`42 42 58 53 45 47 32 00`) |
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
| 96 | 4 | header checksum (profile) over bytes [0, 96) |
| 100 | 3996 | reserved |
| 4096 | body | frames, byte-identical to what the writer committed; unused tail is zero |

Segments are **fully preallocated** at creation (`posix_fallocate`, else zero-fill) and never sparse:
a page fault into a hole under `ENOSPC`/EOF raises `SIGBUS` in the faulting process (S17 proves this).

## 6. `.seal` sidecar (256 B, written once via tmp+fsync+rename)

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | magic `BBXSEAL1` (`42 42 58 53 45 41 4C 31`) |
| 8 | 4 | segment_no |
| 12 | 4 | reason: 1 ROLLED, 2 RUN_END, 3 RECOVERED, 4 SHUTDOWN |
| 16 | 8 | start_seq |
| 24 | 8 | end_seq |
| 32 | 8 | frame_count |
| 40 | 8 | body_len (bytes of valid frames after the 4096 B header) |
| 48 | 4 | body_crc32 over the body bytes `[4096, 4096+body_len)` |
| 52 | 32 | body_hash (BLAKE2b-256 of the same body bytes) |
| 84 | 32 | chain = BLAKE2b-256(prev_chain ‖ body_hash) |
| 116 | 8 | closed_wall_ns (i64) |
| 124 | 16 | sealer (pid u32 ‖ proc_start_ns u64 ‖ 4 reserved) |
| 140 | 112 | reserved |
| 252 | 4 | seal checksum (profile) over bytes [0, 252) |

A segment **with** a valid `.seal` is immutable. A segment **without** one is active-or-dead and is read by the
backward CRC walk; the recoverer writes the missing `.seal` with `reason = RECOVERED`.

## 7. Payload encoding (canonical CBOR subset)

Encoder MUST emit: shortest-form integers, definite lengths only, map keys sorted by their encoded bytes,
UTF-8 text (invalid sequences are base64ed under a `~b64` key), and no tags/indefinite items/floats unless
explicitly allowed. Decoder MUST reject: nesting > 32, maps > 4096 keys, tags, indefinite items, trailing bytes.
Violations raise `CBORError` (`BBX-E151`).

### 7.1 Blob store — `blob/<h[0:2]>/<h[2:4]>/<hash>.bin` (P1-14 / P1-15)

Payloads above `blob_inline_max` (1024 B) leave the frame and become a file named by the BLAKE2b-256 of the
**original** payload; the frame keeps `{"b": <hash>, "n": <orig_len>, "c": <codec>, "s": [start,end]}` (`has_blob`).

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | magic `BBXBLOB1` (`42 42 58 42 4c 4f 42 31`) |
| 8 | 2 | version = 1 |
| 10 | 1 | codec: 0 raw, 1 zlib |
| 11 | 1 | flags (reserved, 0) |
| 12 | 8 | orig_len (u64; the length **before** compression) |
| 20 | body_len | `body` (raw payload, or `zlib.compress(level=6)` of it) |
| 20+body_len | 4 | body_crc32 over `body` as stored |
| 24+body_len | 32 | hash of the original payload |

`body_len = file_size − 56`, so there is no length field to disagree with the file.

**Write path:** `hash → tmp (O_EXCL) → write → fdatasync → os.replace → fsync(dir)`. A visible blob is therefore a
complete blob; a kill before the rename leaves only `*.tmp-*`, swept by `blobs.sweep_tmp`. Two crash stages exist
for tests: `S9` (after the tmp body, before the rename) and `S9b` (after the rename, before the directory fsync).
**Read path:** the CRC is checked before the body is trusted, the codec is dispatched, and the payload hash is
recomputed on every read; a mismatch is `BBX-E162`, a short/garbled container `BBX-E161`, a missing file `BBX-E160`.
The **name is part of the claim** — a file whose content hash differs from its own filename is refused, not
returned.
**Compression** happens on the keeper (`CompressionQueue`), never on the app thread: the blob is stored raw and
rewritten atomically (`tmp` + `replace`) with `codec = 1` above 2 KiB. Readers accept both, and a kill mid-compress
leaves the raw blob readable and a tmp file behind.
**Streams** are chunk-coalesced into 8 KiB blobs (`ChunkCoalescer`), so a 2 000-chunk token stream does not create
2 000 files.

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
