"""Single source of truth for magics, sizes, kinds, flags and defaults (P0-10)."""
from __future__ import annotations

# ── on-disk identity (docs/format.md) ────────────────────────────────────────
MAGIC_META = b"BBXTAPE2"
MAGIC_HEAD = b"BBXHEAD2"
MAGIC_FRAME = b"BBXF"
MAGIC_FRAME_TAIL = b"BBXT"
MAGIC_SEG = b"BBXSEG2\x00"   # 7 chars + NUL pad to 8
MAGIC_SEAL = b"BBXSEAL1"
MAGIC_BLOB = b"BBXBLOB1"

FORMAT_VERSION = 2
MAX_READER_FORMAT_VERSION = 2
SCHEMA_VER = 1

META_BYTES = 4096
META_CRC_OFF = 0x0FF0
HEAD_BYTES = 512
HEAD_SLOT_BYTES = 64
HEAD_SLOT_STRIDE = 0x40
SEGMENT_HEADER_BYTES = 4096
SEGMENT_HEADER_CRC_OFF = 96
SEAL_BYTES = 256
SEAL_CRC_OFF = 252

FRAME_HEADER_BYTES = 48
FRAME_TRAILER_BYTES = 8
FRAME_OVERHEAD_BYTES = FRAME_HEADER_BYTES + FRAME_TRAILER_BYTES  # 56
FRAME_LEN_MULTIPLE = 8
FRAME_MAX_BYTES = 1 << 20            # 1 MiB hard cap (payload is externalised above this)
FRAME_MAX_PAYLOAD = FRAME_MAX_BYTES - FRAME_OVERHEAD_BYTES
RESERVED_HEAD_ROOM = 128             # SEG_END (72 B) + slack, reserved per segment
MIN_SEGMENT_BYTES = 1 << 16          # 64 KiB

# ── head slot layout ────────────────────────────────────────────────────────
HEAD_GEN_OFF = 0x08
HEAD_SEQ_OFF = 0x10
HEAD_OFF_OFF = 0x18
HEAD_TAIL_OFF = 0x20
HEAD_SEALED_OFF = 0x28
HEAD_ACTIVE_SEG_OFF = 0x30
HEAD_FLAGS_OFF = 0x34
HEAD_DROPPED_OFF = 0x38
HEAD_CRC_OFF = 0x3C

# ── meta layout ─────────────────────────────────────────────────────────────
META_VERSION_OFF = 0x08
META_FLAGS_OFF = 0x0A
META_SEGMENT_BYTES_OFF = 0x0C
META_WRITER_UUID_OFF = 0x10
META_CREATED_WALL_OFF = 0x20
META_CREATED_MONO_OFF = 0x28
META_BOOT_ID_HASH_OFF = 0x30
META_PID_OFF = 0x38
META_VANTAGE_OFF = 0x3C
META_PROC_START_OFF = 0x40
META_HOST_FP_OFF = 0x48
META_ATTRS_REF_OFF = 0x68
META_SCHEMA_VER_OFF = 0x70

# ── frame layout ────────────────────────────────────────────────────────────
FRAME_LEN_OFF = 4
FRAME_SEQ_OFF = 8
FRAME_WALL_OFF = 16
FRAME_MONO_OFF = 24
FRAME_KIND_OFF = 32
FRAME_FLAGS_OFF = 34
FRAME_RUN_OFF = 36
FRAME_NODE_OFF = 38
FRAME_PAYLOAD_LEN_OFF = 40
FRAME_CRC_OFF = 44

# ── segment header layout ───────────────────────────────────────────────────
SEG_VERSION_OFF = 8
SEG_FLAGS_OFF = 10
SEG_WRITER_UUID_OFF = 12
SEG_NO_OFF = 28
SEG_START_SEQ_OFF = 32
SEG_SIZE_OFF = 40
SEG_CODEC_OFF = 44
SEG_CREATED_WALL_OFF = 48
SEG_CREATED_MONO_OFF = 56
SEG_PREV_CHAIN_OFF = 64

CODEC_RAW = 0
CODEC_ZLIB = 1
CODEC_ZSTD = 2

# ── .seal layout ────────────────────────────────────────────────────────────
SEAL_NO_OFF = 8
SEAL_REASON_OFF = 12
SEAL_START_SEQ_OFF = 16
SEAL_END_SEQ_OFF = 24
SEAL_FRAME_COUNT_OFF = 32
SEAL_BODY_LEN_OFF = 40
SEAL_BODY_CRC_OFF = 48
SEAL_BODY_HASH_OFF = 52
SEAL_CHAIN_OFF = 84
SEAL_CLOSED_WALL_OFF = 116
SEAL_SEALER_OFF = 124

SEAL_REASON_ROLLED = 1
SEAL_REASON_RUN_END = 2
SEAL_REASON_RECOVERED = 3
SEAL_REASON_SHUTDOWN = 4

# ── flags ───────────────────────────────────────────────────────────────────
FRAME_FLAG_HAS_BLOB = 1 << 0
FRAME_FLAG_TRUNCATED = 1 << 1
FRAME_FLAG_REDACTION_FAILED = 1 << 2
FRAME_FLAG_WRAP_PAD = 1 << 3
PRIORITY_SHIFT = 4
PRIORITY_MAX = 3

HEAD_FLAG_DEGRADED_NO_SPACE = 1 << 0
HEAD_FLAG_DEGRADED_CRC = 1 << 1
HEAD_FLAG_KEEPER_DOWN = 1 << 2
HEAD_FLAG_ROLL_PENDING = 1 << 3

META_FLAG_LAYOUT_RING = 1 << 0
META_FLAG_RESERVED_CHECKSUM = 1 << 2   # must be 0 in v1 (C4); readers reject a tape that sets it

# One checksum in v1: CRC-32/ISO-HDLC through zlib (C speed), check value 0xCBF43926. There is no algorithm
# selector: meta.flags bit 2 is reserved and must be 0. See docs/adr/ADR-004-crc32-not-crc32c.md and DV-005.

# Group-commit threshold (plan §8.2 "publish per batch, not per event"). Measured in
# bench/results/2026-11-14-recovery.json: disk amplification is 1.843x when every event is its own commit,
# 1.263x at 4 KiB, 1.231x at 16 KiB and 1.222x at 64 KiB; the §15.1 row is <= 1.25x, so 16 KiB is the default.
DEFAULT_BATCH_BYTES = 16384

# Latency bound on the group-commit window (C3): publish when the pending bytes reach DEFAULT_BATCH_BYTES, or
# when the oldest unpublished frame is older than DEFAULT_PUBLISH_MS, or when a priority event arrives, or on
# flush()/close(). A keeper thread drains then publishes. The window is measured, not assumed: see
# bench/results/*-writer-*.json "group_commit" rows.
DEFAULT_PUBLISH_MS = 5.0

# Event kinds that must never sit in a commit window: a consumer that sees SEG_END, a GAP or a DEGRADED must be
# able to trust that everything before it is on the tape already.
PRIORITY_KINDS = frozenset({1, 4, 5, 34, 81})   # SEG_END, GAP, DEGRADED, MODEL_CHUNK, RUN_END

# ── event kinds (append-only registry, docs/format.md §4) ───────────────────
KIND_SEG_END = 1
KIND_CHAIN = 2
KIND_INTERN = 3
KIND_GAP = 4
KIND_DEGRADED = 5
KIND_HEARTBEAT = 6
KIND_NODE_OPEN = 16
# A tape can carry nodes from more than one writer: the hook folder numbers its nodes from 1, and the proxy now
# opens a span of its own for every request it forwards (B4). Two writers sharing a numbering space is how two
# unrelated spans become one graph node, so the space is split: hooks may use 1..0x3FFF, the proxy opens from
# 0x4000. Neither side can reach the other's band, and each NODE_OPEN says who owns it.
NODE_SLOT_HOOK_MAX = 0x3FFF
NODE_SLOT_PROXY_BASE = 0x4000
NODE_SLOT_PROXY_MAX = 0x7FFF
KIND_NODE_CLOSE = 17
KIND_EDGE = 18
# 0x13 — the graph range's lowest free id at the time LINK was first written (append-only: MARK keeps the 0x70 it has
# always had here, and a kind is never renumbered to tidy a table). P2 priority by design — §11.10 says the resolver
# writes links at recovery/seal time, not on the hot path — so it is deliberately absent from PRIORITY_KINDS below.
KIND_LINK = 19
KIND_MODEL_REQUEST = 32
KIND_MODEL_RESPONSE = 33
KIND_MODEL_CHUNK = 34
KIND_MODEL_ERROR = 35
KIND_TOOL_REQUEST = 48
KIND_TOOL_RESULT = 49
KIND_RUN_START = 80
KIND_RUN_END = 81
KIND_EXCEPTION = 96
KIND_MARK = 112

KIND_NAMES = {
    KIND_SEG_END: "SEG_END", KIND_CHAIN: "CHAIN", KIND_INTERN: "INTERN", KIND_GAP: "GAP",
    KIND_DEGRADED: "DEGRADED", KIND_HEARTBEAT: "HEARTBEAT", KIND_NODE_OPEN: "NODE_OPEN",
    KIND_NODE_CLOSE: "NODE_CLOSE", KIND_EDGE: "EDGE", KIND_LINK: "LINK",
    KIND_MODEL_REQUEST: "MODEL_REQUEST",
    KIND_MODEL_RESPONSE: "MODEL_RESPONSE", KIND_MODEL_CHUNK: "MODEL_CHUNK",
    KIND_MODEL_ERROR: "MODEL_ERROR", KIND_TOOL_REQUEST: "TOOL_REQUEST",
    KIND_TOOL_RESULT: "TOOL_RESULT", KIND_RUN_START: "RUN_START", KIND_RUN_END: "RUN_END",
    KIND_EXCEPTION: "EXCEPTION", KIND_MARK: "MARK",
}
KIND_FROM_NAME = {v: k for k, v in KIND_NAMES.items()}

# ── defaults (Appendix A) ───────────────────────────────────────────────────
DEFAULT_SEGMENT_BYTES = 16 << 20      # 16 MiB
DEFAULT_MODE = "process"
DEFAULT_CONTENT = "hash"
DEFAULT_SYNC_MS = 250
DEFAULT_SYNC_BYTES = 1 << 20
DEFAULT_TAPE_MAX_BYTES = 256 << 20
DEFAULT_BLOB_INLINE = 1024
DEFAULT_BLOB_MAX = 32 << 20
DEFAULT_QUEUE_MAX = 65536
DEFAULT_SAMPLE = 1.0
MODES = ("volatile", "process", "kernel", "paranoid")
CONTENTS = ("full", "hash", "off")
DURABILITY_CLASS = {"volatile": "F1-weak", "process": "F1", "kernel": "F1+F2", "paranoid": "F1+F2"}
LEASE_MAX_BYTES = 512
LEASE_MAX_AGE_NS = 24 * 3600 * 1_000_000_000

# Every on-disk magic is a fixed-width field; a shorter literal silently shifts the whole record.
BLOB_VERSION = 1
BLOB_HEADER_BYTES = 20               # magic 8 + version 2 + codec 1 + flags 1 + orig_len 8
assert len(MAGIC_META) == 8 and len(MAGIC_HEAD) == 8 and len(MAGIC_SEG) == 8 and len(MAGIC_SEAL) == 8
assert len(MAGIC_FRAME) == 4 and len(MAGIC_FRAME_TAIL) == 4
