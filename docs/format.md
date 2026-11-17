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
`16 NODE_OPEN`, `17 NODE_CLOSE`, `18 EDGE`, `19 LINK`, `32 MODEL_REQUEST`, `33 MODEL_RESPONSE`, `34 MODEL_CHUNK`,
`35 MODEL_ERROR`, `48 TOOL_REQUEST`, `49 TOOL_RESULT`, `80 RUN_START`, `81 RUN_END`, `96 EXCEPTION`, `112 MARK`.

`19 LINK` is the graph range's lowest free id at the time it was first written (§8.4 of the plan names the range and
the kinds in it, not their numbers, and `MARK` keeps the `112` it has always had here — a kind is never renumbered to
tidy a table). Its payload is the record `rec/links.py` resolves (§11.10): `{"class","left_writer","right_writer",
"left_run","right_run","confidence","evidence"}`, CBOR, with a reader that refuses an `inferred` link claiming
confidence 1.0. It is a **P2** kind and is deliberately absent from `PRIORITY_KINDS`: the resolver writes links at
recovery/seal time, never on the hot path. Nothing in this build appends a `LINK` to a live tape — a capsule carries
the resolved records as `links.json` (see `rec/correlate.py`), because appending to a tape to describe it would edit the
evidence the capsule hashes.

#### `graph.json` — nodes and edges (additive in 1.1, D-052)

`graph.json` lives in the run directory (`runs/<tape_key>/graph.json`) and is JSON: `writer_uuid`, `run_id`, `frames`,
`gaps`, `nodes`, `open_nodes`, `run_status`, `status_cause`, `edges`, `edge_summary`. Every node the tape opened appears
in `nodes` (`node_slot`, `name`, `kind`, `parent`, `opened_ns`, `status`, and the `outcome`/`duration_us` its `NODE_CLOSE`
carried); `status` is `closed`, the §14.4 interrupted class, or `unclosed` when the close is not in the scan.

`edges` is derived at seal time from the **same bytes** the capsule receives as `links.json`, and each edge states what
it knows:

| field | values |
|---|---|
| `class` | `exact`, `declared`, `inferred` (the `LINK` class it came from) for run-level edges; `exact` and nothing else for a call edge, which has one kind of evidence |
| `level` | `run` (both ends name a run), `tape` (one end names none), `call` (both ends recorded one call id, D-053), `node` (**not written in this build**) |
| `from` / `to` | `{writer, run_id, node}` — `node` is `null` unless the record's frames sit inside a node's span; a call edge carries `call_id` on the side that wrote it |
| `nodes` | `{from, to}`, `null` on both sides when no node is justified |
| `node_join` | `unknown` (with `node_join_reason`), `exact` (both sides name a node), `partial` (exactly one does — every real call edge measured so far), `ambiguous` (a side's slot is not decidable, with `candidates`) |
| `confidence`, `evidence` | copied from the `LINK` record; a call edge carries `{"rule": "same call id", "carried_by": "x-blackbox-call"}` |

An `exact` link proves two tapes are one run; it does not prove that a node on one tape corresponds to a node on the
other, so no run-level edge names a node and none is invented for a proxied call without a slot. A **call edge** is
stronger and still not a node edge: the same `x-blackbox-call` value on two records means those two records are one
call, and the edge says `level: "call"`. `edge_summary` counts the levels (`total`, `run_level`, `tape_level`,
`call_level`, `node_level`, `node_join`) and carries the same reason, and it counts `ambiguous` joins separately, so the
absence of node-level edges and the presence of undecidable ones are both visible rather than silent. Readers that
predate this change see the keys they already knew and ignore the new ones.

#### `links.json` — `links` and `call_links` (additive in 1.1, D-053)

The capsule carries the resolver's output as `links.json`: `{links: [...], summary, call_links: [...],
call_summary}`. `links`/`summary` are unchanged (run-level records, §11.10); `call_links` holds one record per call id
two writers both recorded — `{call_id, left_writer, right_writer, left_run, right_run, left_node, right_node,
ambiguous, candidates, evidence: {"rule": "same call id", "carried_by": "x-blackbox-call"}}` — and `call_summary`
counts them (`total`, `node_level`, `call_level`, `ambiguous`). A record is `node_level` only when both sides recorded
the call inside a node and the pairing is not ambiguous; `ambiguous` means an id appeared more than once on a side, and
then neither node is named and `candidates` says how many pairings were refused. A reader that predates the two new keys
ignores them, and a capsule written before D-053 simply has none: "no call ids were recorded" is not "no calls were
linked", and the two are separate keys so the difference survives.

#### `x-blackbox-run` and `x-blackbox-call` — what each proves

Both headers are injected by the seam only when the launcher said a proxy is in front (`BLACKBOX_PROXY=1`), and the
proxy strips every `x-blackbox-*` header before the upstream. `x-blackbox-run: <run>` is a claim about a **run**: the
proxy records it as `linked_run`, and the resolver's `exact` link says two tapes belong to one run. `x-blackbox-call:
<16 hex>` is a claim about a **call**: minted per outbound request, recorded by the seam as `call_id` and by the proxy as
`call_id` on its own `MODEL_REQUEST` frame. An app with no proxy in front sends neither, records `call_id: null`, and
loses nothing — there is no second vantage to join. A `MODEL_REQUEST` frame that carries a `call_id` while its writer
is the only writer with that value is a call with one vantage, which is what an unproxied-but-minted id would look
like; the seam does not mint one in that case, so the shape cannot arise.

### 4.0 Model-frame payloads (JSON, not CBOR)

`MODEL_REQUEST` (32), `MODEL_CHUNK` (34), `MODEL_RESPONSE` (33) and `MODEL_ERROR` (35) carry canonical JSON in their
payload; the chunk payload depends on the call's `content` mode (D-030), which the request frame declares:

| mode | `MODEL_CHUNK` payload |
|---|---|
| `full` | the **raw response bytes and nothing else**, so a reader concatenates chunk frames to get the body back |
| `hash` | `[bbx-hash sha256=<64 hex> bytes=<n>]` — a digest of the group's bytes and their count, CPU-readable ASCII |
| `off` | `[bbx-off bytes=<n>]` — the count alone |

The count is in the payload because a reader must be able to answer "how many bytes arrived before the process died"
from the frames alone; `chunk_bytes()` is the one implementation of that rule and `rec.recovery` uses it. A header is
bracketed and begins `[bbx-` so it cannot be mistaken for a body; a body that literally began with `[bbx-` and ended
with `]` and contained ` bytes=<int>]` would be misread, which is the documented cost of the convention (bodies are
SSE or JSON in practice). The summary keys a reader may rely on:

| key | meaning |
|---|---|
| `chunks_events` | parsed stream events the seam counted (seam C counts events, not bytes — D-023) |
| `chunks_raw` | **reads the application made** — counted before redaction, so it is a fact about the call |
| `chunks_stored`, `bytes_stored` | frames and bytes the tape actually holds, after redaction and coalescing |
| `bytes_received` | bytes the application received, unchanged by redaction or by the sink |
| `partial` | the stream ended without reaching the end of the response (cancel, timeout, dropped socket) |
| `redaction` | `{mode, counts: {kind: n}, bytes_in, bytes_out, window, failures}` — **counts, never content** |

The identity keys a `MODEL_REQUEST` carries are additive: `linked_run` (the `x-blackbox-run` value the proxy read off
the request, §11.10) and, since D-053, `call_id` — the `x-blackbox-call` value, written by the seam that minted it and
by the proxy that read it, `null` when there was no proxy to tell.

`chunks_raw` and `chunks_stored` are separate numbers because redaction merges and drops segments; a reader that has
one must not infer the other (D-029). A `redaction.failures` above zero means the redactor raised and its input was
replaced by `[REDACTION_FAILED]`: the call kept its place in the stream and its content is absent, which is what the
architecture's C4 asks for.

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

**Write path (D-019, C32):** `hash → tmp (O_EXCL) → write → os.replace`, and **the calling thread never syncs**.
A visible blob is still a complete blob: `os.replace` of a fully written file is atomic with respect to *process*
death without a sync, because the page cache outlives the process — the same promise the tape makes in `process`
mode. Durability against a *machine* crash is owned by the durability mode, in this order:

| mode | who syncs the blob | when, relative to the referencing frame |
|---|---|---|
| `process` | nobody | it goes as far as the tape goes: kill-safe, not power-cut-safe |
| `kernel` | the keeper | `sync_pending()` in the tick, **before** the tape sync covering the frame (`Keeper.tick` → `sync_pending` → `publish_if_due`); `TapeWriter._maybe_sync` repeats the same order if a group commit publishes on the calling thread |
| `paranoid` | the caller, as part of the put | the blob **and its directory** are durable before the frame is appended, so before it can be published |

The invariant is *no durable frame references a non-durable blob*, and it is a test rather than a comment:
`tests/test_blob_durability.py` records the sync calls in order through a platform shim and reads the published
frames back to check every `has_blob` reference against what was synced. A kill before the rename leaves only
`*.tmp-*`, swept by `blobs.sweep_tmp`. Two crash stages exist for tests: `S9` (after the tmp body, before the
rename) and `S9b` (after the rename, before any directory sync).
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

## 8.1 Capsule `.bbx` v0 (P2-10)

ZIP, STORED for raw segment bodies and DEFLATE for JSON, entries sorted with `manifest.json` **last**:

| Entry | Content |
|---|---|
| `events/seg-000001.raw` | the segment's bytes, byte-identical to the tape |
| `chain.json` | `{chain_root, per_segment}` — what a reader needs to walk across a retention hole |
| `manifest.json` | identity, `run` block (status, `status_cause`, completeness, `truncated_tail`, `lost_from_seq`), `gaps`, `counts`, `integrity {alg, entries, root}`, `signature: null` |
| `INTEGRITY.sha256` | `<sha256>  <name>` per entry except itself and `manifest.json`, sorted |
| `graph.json` | the run's nodes and edges, byte-identical to the run directory's copy (D-052, entry since D-053) |
| `events/events.jsonl` | the timeline, one JSON object per frame, derived from the redacted frames (P4-13, D-054) |
| `otlp/spans.json` | the same frames as OTLP/JSON ResourceSpans: agent, inference and tool spans (P4-14, D-056) |
| `links.json` | the resolved `LINK` records and, since D-053, the `call_links` |

`integrity.root = sha256(INTEGRITY.sha256)` — the manifest can therefore carry the root without hashing itself,
while every data byte is still covered. Every entry's timestamp is `1980-01-01 00:00:00`, every mode is `0o644`,
`create_system = 0`, so re-sealing the same inputs is **byte-identical** (the byte-identity `I6` needs). The file is
written as `.<id>.partial` with `O_EXCL`, fsynced, `os.replace`d, then the directory is fsynced: a visible `.bbx`
is a complete `.bbx`. Reading validates names (absolute, `..`, backslash, NUL), case-folded duplicates, sizes,
entry-count and ratio **before** it trusts a single entry.

**Derived parts.** `graph.json`, `events/events.jsonl` and `otlp/spans.json` are listed in `manifest.derived_parts[]` as
`{name, sha256, generator}` — the hash of the exact bytes written into the capsule — sorted by name so the manifest is a
function of its inputs. `blackbox capsule verify <capsule>` recomputes each part from the capsule's own `events/*.raw`
(`cap/derived.py`): the graph key by key (`COMPARED`: nodes, edges, counts, frames, status), each export byte for byte,
reporting the first line that differs. A mismatch is `BBX-E412` and exits 1, an unreadable capsule exits 2, a capsule
that declares no derived parts verifies as `absent` and exits 0, and a part this build cannot recompute is listed as
`unknown` — printed, never counted as a pass. Verification never rewrites anything: a capsule whose part has drifted is
reported, not repaired, and the raw frames it was derived from are untouched.

#### `events/events.jsonl` — the timeline, one object per frame (P4-13, D-054)

One line per frame, `LF`-terminated with a final newline, keys sorted, no generation timestamp: the file is a pure
function of the frames, which is what makes `capsule verify` a comparison instead of a hope. Each object is

`{"seq", "kind", "kind_id", "time_unix_nano", "bbx_mono_nano", "segment", "node_slot", "run_slot", "attributes"}`

and every attribute name is either a **pinned OpenTelemetry semconv name** or one of ours:

| attribute | when | source |
|---|---|---|
| `gen_ai.operation.name` | a `MODEL_REQUEST` whose path is an API the mapping knows (`chat`, `text_completion`) | semconv **v1.39.0** (`6d05e92`) |
| `gen_ai.provider.name` | the recorded provider host is in the explicit table (`api.openai.com` → `openai`, `api.anthropic.com` → `anthropic`, `generativelanguage.googleapis.com` → `gcp.gemini`, `aiplatform.googleapis.com` → `gcp.vertex_ai`, and the others in `export_jsonl.SYSTEMS`) | semconv **v1.39.0** (`6d05e92`) |
| `gen_ai.request.model` | the frame recorded a model | semconv **v1.39.0** (`6d05e92`) |
| `bbx.model.*`, `bbx.node.*`, `bbx.run.*`, `bbx.gap.*`, `bbx.chain.*`, `bbx.chunk.*`, `bbx.payload`, `bbx.redaction` | everything else | this project |

Three things it deliberately does not do. It does not **guess a provider**: an address that is not a known provider gets
no `gen_ai.provider.name`, and the host it dialled is still written as `bbx.provider.host`. It does not **copy content**: a
`MODEL_CHUNK` line carries its byte count, and the `[bbx-hash …]`/`[bbx-off …]` tag when the sink wrote one, never the
bytes — the export is a timeline, and the bytes stay in the segments under the content policy. And it does not **emit
`error.type`**: the tape records an error *message*, and a message is not a type, so the string is carried as
`bbx.model.error` rather than dressed as a semconv value. Payloads of kinds the mapping says nothing about are carried
under `bbx.payload`, or as a digest and a length when they exceed 2 KiB.

#### `otlp/spans.json` — the same frames as OTLP/JSON (P4-14, D-056)

One `ExportTraceServiceRequest` in proto3 JSON: `resourceSpans[0].resource.attributes` names the capsule
(`service.name = "blackbox"`, `bbx.capsule.id`), `scopeSpans[0].scope` is `blackbox 0.1.0.dev0`, and `spans` holds the
run. Kinds are OTLP's (`1` INTERNAL, `3` CLIENT), status codes are `0`/`1`/`2`, int64 values are strings, ids are
lower-case hex. The same **semconv v1.39.0** pin as the JSONL export, because two exports of one capsule must not
describe one run in two vocabularies.

| frames | span | name | kind |
|---|---|---|---|
| `RUN_START` … `RUN_END` | the root | `bbx.run` | INTERNAL |
| `NODE_OPEN` … `NODE_CLOSE`, kind `tool` | tool | `execute_tool {gen_ai.tool.name}` | INTERNAL |
| any other node pair | agent | `invoke_agent {gen_ai.agent.name}`, or `invoke_agent` when the node has no name | INTERNAL |
| `MODEL_REQUEST` … `MODEL_RESPONSE`/`MODEL_ERROR` | inference | `{gen_ai.operation.name} {gen_ai.request.model}`, each half optional | CLIENT |
| a call whose path is not in the mapping | inference | `bbx.model.call` — no semconv name, because there is no operation to name | CLIENT |

Span attributes are the same two families as the JSONL file: the pinned `gen_ai.*` names, and ours
(`bbx.node.slot/kind/duration_us`, `bbx.call.id`, `bbx.model.*` counters, `bbx.request.fp`, `bbx.response.fp`,
`bbx.response.status`, `bbx.span.source`, `bbx.span.truncated`, `bbx.span.partial`, `bbx.model.error`). Parents follow
the recording: a model call's parent is the node span of its slot, a node's parent is the span of its `parent` slot, and
the root is `bbx.run`.

**Ids are derived, not drawn.** `traceId = sha256(run_id)[:32]`, so two vantages of one run share a trace — the property
P3B-03 wants and does not yet have at node level (`docs/node-edges.md`). `spanId = sha256(capsule_id:seq)[:16]` for the
frame that opened the span, so two capsules of one run cannot collide and the same capsule always renders the same ids.
The writer's uuid is deliberately unused: the capsule does not carry it, and a derived part must be recomputable from what
the capsule holds.

**Pairing.** A response is paired with its request by `bbx.call.id` when both carry it — the response frame repeats the
call id and linked run since P4-14 — and by tape order per node slot when they do not (a tape written before that change).
A pair that never closes gets a span that ends at the tape's last frame with `bbx.span.truncated: true` and an error
status that says which frame is missing: a node with no close is a fact about the run, and dropping its span would lose
it.

**Absent on purpose.** No token counts, no `gen_ai.response.finish_reasons`, no request parameters: this build does not
decode response bodies (P3-08 is open), and a number invented from a byte count would be the over-claim everything else
here refuses. No prompt or completion text — counters only, by the same rule as the JSONL export.

**Declared and recomputed** like the other derived parts: sha256 in `manifest.derived_parts[]`, `capsule verify`
re-renders and compares bytes, an edit is `BBX-E412` with the first differing line, and nothing is rewritten.

**What was and was not checked.** The document validates against `tests/schemas/otlp-spans.schema.json` (the subset of
OTLP/JSON this build produces), checked by `tests/_schema.py` — a validator that implements that subset and raises on a
keyword it does not implement, so the snapshot cannot grow a check nobody runs. The ticket also asks that "a
Phoenix/Langfuse-class importer accepts the file": **that check was not performed here** — no importer of that class runs
in this environment, and saying one accepted it would be a claim without a witness. It stays a manual step, recorded in
`docs/STATE.json` under `open_blockers`, and the file is written to the documented OTLP/JSON shape so that the check is a
test of an importer rather than of a bespoke format.

## 9. Conformance vectors

`tests/conformance/vectors/*.bin` + `index.json`: a meta page, a head page, a 3-frame segment, a `.seal`, and the
expected decoded dicts. `tests/conformance/test_conformance.py` decodes them with an independent parser written
from this document.
