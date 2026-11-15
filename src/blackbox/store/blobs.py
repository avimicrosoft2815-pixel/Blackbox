"""Content-addressed blob store (P1-14 write/read path, P1-15 compression queue).

Layout and file format are frozen in `docs/format.md` §8.8:

    <data_dir>/blob/<h[0:2]>/<h[2:4]>/<hash>.bin
    BBXBLOB1 (8) ‖ version u16 ‖ codec u8 ‖ flags u8 ‖ orig_len u64 ‖ body ‖ body_crc32 u32 ‖ hash 32 B

`hash` is BLAKE2b-256 of the **original** payload, `body` is the payload as stored (`codec` 0 = raw, 1 = zlib),
and `body_crc32` covers the stored body, so a reader can tell a corrupt byte from a wrong hash. Nothing here maps a
file, opens a bare descriptor, or trusts a name: every read re-verifies the hash, and every write is
`tmp` → `fsync` → `os.replace` → `fsync(dir)`, so a visible blob is a complete blob.

Two things are deliberately **not** on the writer thread:

* compression (P1-15) — `put` stores raw and the keeper drains `CompressionQueue`, which rewrites the file
  atomically with `codec = 1`; readers accept both, and a kill mid-compress leaves the raw blob untouched;
* deletion — a blob is collected by the mark-and-sweep GC (§9.9), never by this module.

Failure policy: a blob that cannot be written raises (the recorder decides whether to degrade), but the
compression queue never raises into the keeper — it counts the failure and keeps the raw blob readable.
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass, field

from blackbox import constants as C
from blackbox.codec.crc32 import crc32
from blackbox.codec.ids import blake2b
from blackbox.errors import BlackBoxError, bbx
from blackbox.platform import current as _plat_current

_plat = _plat_current()

MAGIC = C.MAGIC_BLOB
VERSION = C.BLOB_VERSION
_HEADER = struct.Struct("<8sHBBQ")          # magic, version, codec, flags, orig_len
assert _HEADER.size == C.BLOB_HEADER_BYTES, _HEADER.size

CODEC_RAW = 0
CODEC_ZLIB = 1
CODEC_NAMES = {CODEC_RAW: "raw", CODEC_ZLIB: "zlib"}

INLINE_MAX_BYTES = 1024          # §8.8: at or below this, the payload stays inside the frame
COMPRESS_MIN_BYTES = 2048        # §8.8: zlib-6 above 2 KiB, on the keeper, never on the writer thread
ZLIB_LEVEL = 6
CHUNK_COALESCE_BYTES = 8192      # §8.8: a stream becomes whole blobs, not one blob per token

DIE_ENV = "BLACKBOX_DIE_AT"


def _die(stage: str) -> None:
    """Crash hook: `BLACKBOX_DIE_AT=<stage>` makes the process die here, like `kill -9`. Unused in production."""
    if os.environ.get(DIE_ENV) == stage:
        os._exit(137)          # the same code the tape crash harness uses for "died of a stage"


def blob_root(data_dir: str) -> str:
    return os.path.join(data_dir, "blob")


def digest_of(payload: bytes | memoryview) -> bytes:
    return blake2b(bytes(payload))


def blob_path(data_dir: str, digest: bytes | str) -> str:
    """`blob/xx/yy/<hash>.bin` — two levels of fan-out so one directory never holds a whole store."""
    h = digest.hex() if isinstance(digest, bytes) else digest.lower()
    if len(h) != 64 or any(ch not in "0123456789abcdef" for ch in h):
        raise ValueError(f"blob hash must be 64 hex chars, got {h!r}")
    return os.path.join(blob_root(data_dir), h[0:2], h[2:4], f"{h}.bin")


def should_inline(payload: bytes | memoryview) -> bool:
    """The recorder keeps small payloads inside the frame; this is the one threshold both sides use."""
    return len(payload) <= INLINE_MAX_BYTES


# ── the container ───────────────────────────────────────────────────────────
def encode(digest: bytes, body: bytes, *, codec: int = CODEC_RAW, orig_len: int | None = None) -> bytes:
    """Encode one blob file. `digest` is of the original payload, `body` is what actually gets stored."""
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    n = len(body) if orig_len is None else orig_len
    out = bytearray(_HEADER.size + len(body) + 4 + 32)
    _HEADER.pack_into(out, 0, MAGIC, VERSION, codec, 0, n)
    out[_HEADER.size:_HEADER.size + len(body)] = body
    crc_at = _HEADER.size + len(body)
    struct.pack_into("<I", out, crc_at, crc32(body))
    out[crc_at + 4:crc_at + 4 + 32] = digest
    return bytes(out)


def decode(raw: bytes, *, verify_hash: bool = True) -> dict:
    """Decode and verify one blob file. Raises BBX-E161/E162 rather than returning a half-trusted body."""
    if len(raw) < _HEADER.size + 4 + 32:
        raise bbx("BBX-E161", f"blob is {len(raw)} B, shorter than the header + trailer")
    magic, version, codec, _flags, orig_len = _HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise bbx("BBX-E161", f"bad blob magic {magic!r}")
    if version != VERSION:
        raise bbx("BBX-E161", f"blob version {version} is not {VERSION}")
    body_len = len(raw) - _HEADER.size - 4 - 32
    body = raw[_HEADER.size:_HEADER.size + body_len]
    (want_crc,) = struct.unpack_from("<I", raw, _HEADER.size + body_len)
    digest = raw[_HEADER.size + body_len + 4:]
    got_crc = crc32(body)
    if got_crc != want_crc:
        raise bbx("BBX-E162", f"stored body CRC {got_crc:08x} != header {want_crc:08x}")
    if codec == CODEC_RAW:
        payload = body
    elif codec == CODEC_ZLIB:
        try:
            payload = zlib.decompress(body)
        except zlib.error as exc:
            raise bbx("BBX-E162", f"zlib body does not decompress: {exc}") from None
    else:
        raise bbx("BBX-E156", f"blob codec {codec} ({CODEC_NAMES.get(codec, 'unknown')}) is not available")
    if len(payload) != orig_len:
        raise bbx("BBX-E162", f"decompressed length {len(payload)} != header orig_len {orig_len}")
    if verify_hash and blake2b(payload) != digest:
        raise bbx("BBX-E162", f"payload hash {blake2b(payload).hex()[:16]} != header {digest.hex()[:16]}")
    return {"payload": payload, "codec": codec, "codec_name": CODEC_NAMES.get(codec, str(codec)),
            "orig_len": orig_len, "stored_len": body_len, "hash": digest}


# ── write path (P1-14) ──────────────────────────────────────────────────────
def _write_atomic(path: str, blob: bytes) -> str:
    """tmp → fsync → replace → fsync(dir). A visible blob is therefore a complete blob."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _plat.pwrite(fd, memoryview(blob), 0)
        _die("S9")                      # tmp body is on disk, the name is not: a kill leaves nothing visible
        if hasattr(os, "fdatasync"):
            os.fdatasync(fd)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _die("S9b")                         # the blob is visible; only the directory entry is not yet durable
    _plat.fsync_dir(directory)
    return path


def put(data_dir: str, payload: bytes, *, compress: bool = False) -> dict:
    """Write one blob. Returns its ref record; identical payloads cost one file (`deduped`).

    `compress=True` compresses inline — used by tests and by callers that already know the payload is cold. The
    default is raw, because compression belongs on the keeper (P1-15), not on the writer thread.
    """
    digest = digest_of(payload)
    path = blob_path(data_dir, digest)
    if os.path.exists(path):
        return {"hash": digest, "path": path, "codec": CODEC_RAW, "orig_len": len(payload),
                "stored_len": os.path.getsize(path), "deduped": True, "inline": should_inline(payload)}
    codec, body = CODEC_RAW, payload
    if compress:
        codec, body = CODEC_ZLIB, zlib.compress(payload, ZLIB_LEVEL)
        if len(body) >= len(payload):
            codec, body = CODEC_RAW, payload      # compression that grows the file is not compression
    _write_atomic(path, encode(digest, body, codec=codec, orig_len=len(payload)))
    return {"hash": digest, "path": path, "codec": codec, "codec_name": CODEC_NAMES[codec],
            "orig_len": len(payload), "stored_len": len(body), "deduped": False,
            "inline": should_inline(payload)}


def read_bytes(data_dir: str, digest: bytes | str) -> bytes:
    """The stored container bytes (header included), unverified — used by the compression queue and doctors."""
    path = blob_path(data_dir, digest)
    with open(path, "rb") as fh:
        return fh.read()


def get(data_dir: str, digest: bytes | str, *, verify_hash: bool = True) -> dict:
    """Read one blob, verifying CRC **and** hash on every read. A missing blob is BBX-E160, not `None`."""
    path = blob_path(data_dir, digest)
    try:
        raw = read_bytes(data_dir, digest)
    except FileNotFoundError:
        raise bbx("BBX-E160", f"{path} does not exist") from None
    out = decode(raw, verify_hash=verify_hash)
    want = digest if isinstance(digest, bytes) else bytes.fromhex(digest)
    if out["hash"] != want:
        # The name is part of the claim: a file that is internally consistent but sits under another hash is a
        # wrong blob, and returning it would make every later `verify` agree with the mistake.
        raise bbx("BBX-E162", f"{path} contains {out['hash'].hex()[:16]} but is named {want.hex()[:16]}")
    out["path"] = path
    return out


def ref_stub(record: dict, *, span: tuple[int, int] | None = None) -> dict:
    """The CBOR-friendly stub a frame carries when its payload lives in the store (§8.5, `has_blob`)."""
    stub = {"b": record["hash"], "n": record["orig_len"], "c": record["codec"]}
    if span is not None:
        stub["s"] = [int(span[0]), int(span[1])]
    return stub


def sweep_tmp(data_dir: str, *, max_age_s: float = 0.0, now: float | None = None) -> list[str]:
    """Remove `.tmp-*` leftovers from writes that died before `os.replace`. Returns the paths removed."""
    stamp = time.time() if now is None else now
    removed: list[str] = []
    root = blob_root(data_dir)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if ".tmp-" not in name:
                continue
            path = os.path.join(dirpath, name)
            try:
                age = stamp - os.stat(path).st_mtime
            except FileNotFoundError:
                continue
            if age < max_age_s:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            removed.append(path)
    return removed


class ChunkCoalescer:
    """Accumulates stream chunks into whole blobs of at most `CHUNK_COALESCE_BYTES`.

    A token stream can emit thousands of tiny chunks; storing each as its own blob would triple the file count and
    the directory work per token. Chunks are therefore buffered and flushed whole, and `close()` alone can leave a
    final short blob — which is the only blob a reader can see partially written in *time*, never in *bytes*.
    """

    def __init__(self, data_dir: str, *, coalesce_bytes: int = CHUNK_COALESCE_BYTES) -> None:
        self.data_dir = data_dir
        self.coalesce_bytes = coalesce_bytes
        self._buf = bytearray()
        self.closed_blobs: list[dict] = []

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def append(self, chunk: bytes) -> dict | None:
        """Add a chunk; returns the record when this call completed a blob, else `None`."""
        self._buf += chunk
        if len(self._buf) < self.coalesce_bytes:
            return None
        return self._flush()

    def _flush(self) -> dict | None:
        if not self._buf:
            return None
        rec = put(self.data_dir, bytes(self._buf))
        self.closed_blobs.append(rec)
        self._buf = bytearray()
        return rec

    def close(self) -> dict | None:
        """Flush what is left; a coalescer that emitted nothing returns `None` rather than an empty blob."""
        return self._flush()


# ── compression queue (P1-15) ───────────────────────────────────────────────
@dataclass
class CompressionQueue:
    """Keeper-driven, one job per blob, no partial file visible to a reader.

    `enqueue` is the only thing the writer thread may do (a dict insert). `drain` runs on the keeper: it reads
    the raw blob, compresses it, and calls `os.replace` with a fully written tmp file. A reader therefore sees
    either the raw blob or the compressed one, never a mixture; and a failure leaves the raw blob in place.
    """

    data_dir: str
    min_bytes: int = COMPRESS_MIN_BYTES
    pending: dict[str, dict] = field(default_factory=dict)
    done: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_saved: int = 0
    last_errors: list[dict] = field(default_factory=list)

    def enqueue(self, digest: bytes | str, *, orig_len: int | None = None) -> bool:
        h = digest.hex() if isinstance(digest, bytes) else digest.lower()
        if h in self.pending:
            return False
        self.pending[h] = {"hash": h, "orig_len": orig_len}
        return True

    def __len__(self) -> int:
        return len(self.pending)

    def drain(self, limit: int = 64) -> dict:
        """Compress up to `limit` blobs. Never raises: failures are counted and the raw blob stays readable."""
        jobs = sorted(self.pending)[:limit] if limit else sorted(self.pending)
        for h in jobs:
            try:
                outcome = self.compress_one(h)
            except (BlackBoxError, OSError) as exc:      # missing blob / IO: skip, keep it raw
                self.skipped += 1
                self.last_errors.append({"hash": h, "error": f"{type(exc).__name__}: {exc}"})
                self.pending.pop(h, None)
                continue
            except Exception as exc:                                  # never let the keeper die on a blob
                self.failed += 1
                self.last_errors.append({"hash": h, "error": f"{type(exc).__name__}: {exc}"})
                self.pending.pop(h, None)
                continue
            self.pending.pop(h, None)
            if outcome["action"] == "compressed":
                self.done += 1
                self.bytes_saved += outcome["saved"]
            else:
                self.skipped += 1
        return self.stats()

    def compress_one(self, digest: bytes | str) -> dict:
        """Compress one blob in place, atomically. Returns `{"action": compressed|kept, "saved": n}`."""
        record = get(self.data_dir, digest)
        if record["codec"] != CODEC_RAW:
            return {"action": "kept", "saved": 0, "reason": "already compressed"}
        payload = record["payload"]
        if len(payload) < self.min_bytes:
            return {"action": "kept", "saved": 0, "reason": f"below {self.min_bytes} B"}
        body = zlib.compress(payload, ZLIB_LEVEL)
        if len(body) >= len(payload):
            return {"action": "kept", "saved": 0, "reason": "compression does not shrink this payload"}
        path = blob_path(self.data_dir, record["hash"])
        _write_atomic(path, encode(record["hash"], body, codec=CODEC_ZLIB, orig_len=record["orig_len"]))
        return {"action": "compressed", "saved": len(payload) - len(body)}

    def stats(self) -> dict:
        return {"pending": len(self.pending), "compressed": self.done, "skipped": self.skipped,
                "failed": self.failed, "bytes_saved": self.bytes_saved,
                "errors": self.last_errors[-8:]}
