"""The hot path: append frames into the mmap'd active segment and publish a two-slot head (P1-08).

Guarantees this file is allowed to make (plan §10):
  * `mode=process` → **F1** (process death) for committed frames, with no syscall on the append path.
  * `mode=kernel`/`paranoid` → additionally group-commit `fdatasync` for F2.
It never claims F3, and never raises into user code: allocation failure degrades (P1-20).
"""
from __future__ import annotations

import mmap
import os
import time

from blackbox import constants as C
from blackbox.errors import bbx
from blackbox.tape import chain as chain_mod
from blackbox.tape import frame as frame_mod
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import prealloc, segments
from blackbox.codec.ids import new_writer_uuid

DIE_ENV = "BLACKBOX_DIE_AT"


class TapeError(RuntimeError):
    pass


def _die_hook() -> str | None:
    return os.environ.get(DIE_ENV)


def _die(stage: str) -> None:
    """Crash the process at a named write stage, exactly like `kill -9` (no atexit, no flush)."""
    if os.environ.get(DIE_ENV) == stage:
        os._exit(137)


class TapeWriter:
    def __init__(self, data_dir: str, *, writer_uuid: bytes | None = None,
                 segment_bytes: int = C.DEFAULT_SEGMENT_BYTES, mode: str = C.DEFAULT_MODE,
                 vantage: int = 1, checksum_algo: int = C.CHECKSUM_CRC32C,
                 chain_enabled: bool = True, sealer_token: bytes = b"") -> None:
        if mode not in C.MODES:
            raise bbx("BBX-E100", f"unknown mode {mode!r}")
        if segment_bytes < C.MIN_SEGMENT_BYTES:
            raise bbx("BBX-E130", f"segment_bytes {segment_bytes} < {C.MIN_SEGMENT_BYTES}")
        self.data_dir = data_dir
        self.writer_uuid = writer_uuid or new_writer_uuid()
        self.segment_bytes = segment_bytes
        self.mode = mode
        self.vantage = vantage
        self.checksum_algo = checksum_algo
        self.chain_enabled = chain_enabled
        self.sealer_token = sealer_token or b""

        self.tape_dir = os.path.join(data_dir, "tape", self.writer_uuid.hex())
        self.meta_path = os.path.join(self.tape_dir, "meta")
        self.head_path = os.path.join(self.tape_dir, "head")

        self._slot: head_mod.HeadSlot | None = None
        self._mm: mmap.mmap | None = None
        self._seg_fd: int | None = None
        self._seg_path: str | None = None
        self._chain = chain_mod.ChainState()
        self._batch: list[bytes] = []
        self._batch_first_seq = 0
        self._frame_count = 0
        self._last_sync_ns = time.perf_counter_ns()
        self._unpublished_bytes = 0
        self.degraded_reason: str | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def open(self) -> "TapeWriter":
        os.makedirs(self.tape_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(self.meta_path):
            blob = meta_mod.encode(writer_uuid=self.writer_uuid, segment_bytes=self.segment_bytes,
                                   vantage=self.vantage, checksum_algo=self.checksum_algo)
            with open(self.meta_path, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        meta = meta_mod.decode(self._read_meta())
        self.checksum_algo = meta["checksum_algo"]
        self.segment_bytes = meta["segment_bytes"]

        existing = segments.list_segments(self.tape_dir)
        best = head_mod.read_best(self.head_path)
        if best is None:
            if existing:
                raise TapeError("segments exist but the head is unreadable; run recovery")
            if not os.path.exists(self.head_path):
                with open(self.head_path, "wb") as fh:
                    fh.write(head_mod.empty_head_file())
                    fh.flush()
                    os.fsync(fh.fileno())
            self._slot = head_mod.HeadSlot(head_off=C.SEGMENT_HEADER_BYTES, active_segment=1)
            try:
                self._create_segment(1)
            except prealloc.PreallocError as exc:
                self.degraded_reason = "NO_SPACE"
                raise bbx("BBX-E140", str(exc)) from exc
            self._map_segment(1)
            self._publish(publish_chain=False)
        else:
            self._slot = best
            n = best.active_segment
            if not os.path.exists(segments.seg_path(self.tape_dir, n)):
                raise TapeError(f"active segment {n} missing")
            self._map_segment(n)
            self._chain.value = self._segment_prev_chain(n)
            # a reopened writer must never reuse seq numbers
            self._frame_count = 0
        return self

    def _read_meta(self) -> bytes:
        with open(self.meta_path, "rb") as fh:
            return fh.read(C.META_BYTES)

    def _segment_prev_chain(self, n: int) -> bytes:
        with open(segments.seg_path(self.tape_dir, n), "rb") as fh:
            hdr = segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))
        return hdr["prev_chain"]

    def _create_segment(self, n: int) -> None:
        prev = self._chain.value
        segments.create(self.tape_dir, n, writer_uuid=self.writer_uuid,
                        segment_bytes=self.segment_bytes, prev_chain=prev)

    def _map_segment(self, n: int) -> None:
        if self._mm is not None:
            self._mm.flush()
            self._mm.close()
        if self._seg_fd is not None:
            os.close(self._seg_fd)
        path = segments.seg_path(self.tape_dir, n)
        self._seg_path = path
        self._seg_fd = os.open(path, os.O_RDWR)
        self._mm = mmap.mmap(self._seg_fd, self.segment_bytes)

    # ── append / publish ────────────────────────────────────────────────────
    def append(self, kind: int, payload: bytes = b"", *, priority: int = 1, flags: int = 0,
               run_slot: int = 0, node_slot: int = 0, wall_ns: int | None = None,
               mono_ns: int | None = None) -> int:
        assert self._slot is not None and self._mm is not None
        size = frame_mod.frame_size(len(payload))
        if size > self.segment_bytes - C.SEGMENT_HEADER_BYTES - C.RESERVED_HEAD_ROOM:
            raise bbx("BBX-E225", f"frame of {size} B does not fit a {self.segment_bytes} B segment")
        if self._slot.head_off + size + C.RESERVED_HEAD_ROOM > self.segment_bytes:
            self.roll()

        seq = self._slot.head_seq + 1
        blob = frame_mod.encode(seq, kind, payload, flags=flags, priority=priority, run_slot=run_slot,
                                node_slot=node_slot, wall_ns=wall_ns, mono_ns=mono_ns,
                                algo=self.checksum_algo)
        off = self._slot.head_off
        body_start = C.FRAME_HEADER_BYTES
        pad = frame_mod.padded_len(len(payload))

        # payload+padding first, then trailer, then header: a torn write leaves no parseable header
        self._mm[off + body_start:off + body_start + pad] = blob[body_start:body_start + pad]
        _die("S1")
        self._mm[off + size - C.FRAME_TRAILER_BYTES:off + size] = blob[size - C.FRAME_TRAILER_BYTES:]
        _die("S2")
        self._mm[off:off + C.FRAME_HEADER_BYTES] = blob[:C.FRAME_HEADER_BYTES]
        _die("S3")

        if not self._batch:
            self._batch_first_seq = seq
        self._batch.append(blob)
        self._frame_count += 1
        self._unpublished_bytes += size
        self._slot = self._slot.with_(head_seq=seq, head_off=off + size)
        return seq

    def publish(self, *, publish_chain: bool = True) -> head_mod.HeadSlot:
        """Publish the current head. Also commits a CHAIN frame for the batch when enabled."""
        assert self._slot is not None
        if publish_chain and self.chain_enabled and self._batch:
            batch_hash = chain_mod.batch_hash(self._batch)
            # Decide the roll *before* the CHAIN frame is encoded: the frame carries `last_seq`, and a roll
            # advances `head_seq` (the SEG_END frame), so encoding first would duplicate a seq number.
            sized = frame_mod.frame_size(len(chain_mod.encode_payload(
                prev=self._chain.value, batch=batch_hash, first_seq=self._batch_first_seq,
                last_seq=self._slot.head_seq)))
            if self._slot.head_off + sized + C.RESERVED_HEAD_ROOM > self.segment_bytes:
                self.roll()
            payload = chain_mod.encode_payload(prev=self._chain.value, batch=batch_hash,
                                               first_seq=self._batch_first_seq,
                                               last_seq=self._slot.head_seq)
            blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_CHAIN, payload, priority=0,
                                    algo=self.checksum_algo)
            off = self._slot.head_off
            self._mm[off:off + len(blob)] = blob
            self._chain.value = chain_mod.chain_value(self._chain.value, batch_hash)
            self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
            self._frame_count += 1
        self._slot = self._publish()

        if self.mode in ("kernel", "paranoid"):
            self._maybe_sync(force=(self.mode == "paranoid"))
        self._batch = []
        self._batch_first_seq = 0
        return self._slot

    def _publish(self, *, publish_chain: bool = True) -> head_mod.HeadSlot:
        assert self._slot is not None
        _die("S3")
        slot = head_mod.publish(self.head_path, self._slot, mode=self.mode, die_point=_die)
        _die("S5")
        self._slot = slot
        return slot

    def _maybe_sync(self, *, force: bool = False) -> None:
        if self._seg_fd is None:
            return
        now = time.perf_counter_ns()
        due = (now - self._last_sync_ns) >= 250 * 1_000_000 or self._unpublished_bytes >= (1 << 20)
        if not (force or due):
            return
        if self._mm is not None:
            self._mm.flush()
        if hasattr(os, "fdatasync"):
            os.fdatasync(self._seg_fd)
        else:
            os.fsync(self._seg_fd)
        self._last_sync_ns = now
        self._unpublished_bytes = 0

    def append_and_publish(self, kind: int, payload: bytes = b"", **kw) -> int:
        seq = self.append(kind, payload, **kw)
        self.publish()
        return seq

    # ── roll ────────────────────────────────────────────────────────────────
    def roll(self) -> None:
        """Close the active segment and start the next one (P1-09, §8.6)."""
        assert self._slot is not None and self._mm is not None
        n = self._slot.active_segment
        # 1. SEG_END commits the current head
        blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0,
                                algo=self.checksum_algo)
        if self._slot.head_off + len(blob) <= self.segment_bytes:
            off = self._slot.head_off
            self._mm[off:off + len(blob)] = blob
            self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
        self._publish()
        _die("S14")     # after SEG_END published, before the next segment is durable

        # 2. capture the body of segment n and seal it (footer sidecar; P1-13)
        body = bytes(self._mm[C.SEGMENT_HEADER_BYTES:self._slot.head_off])
        start_seq, end_seq, frame_count = self._segment_range(n)
        self._mm.flush()
        self._mm.close()
        self._mm = None
        if self._seg_fd is not None:
            os.close(self._seg_fd)
            self._seg_fd = None
        seal_info = self._write_seal(n, body=body, start_seq=start_seq, end_seq=end_seq,
                                     frame_count=frame_count, reason=C.SEAL_REASON_ROLLED)
        if seal_info is not None:
            self._chain.value = seal_info["chain"]     # the next segment's header chains to this seal

        # 3. preallocate + header the next segment. Failure degrades; it never raises into the app.
        next_no = n + 1
        try:
            self._create_segment(next_no)
        except prealloc.PreallocError:
            self.degraded_reason = "NO_SPACE"
            self._slot = self._slot.with_(flags=self._slot.flags | C.HEAD_FLAG_DEGRADED_NO_SPACE)
            self._publish()
            raise bbx("BBX-E140", "preallocation failed at roll; recording degraded") from None
        _die("S14b")
        self._map_segment(next_no)
        _die("S15")     # segment n+1 exists (and its header is durable) but the head still names n
        self._slot = self._slot.with_(active_segment=next_no, head_off=C.SEGMENT_HEADER_BYTES,
                                      flags=self._slot.flags & ~C.HEAD_FLAG_ROLL_PENDING)
        self._publish()

    def _write_seal(self, n: int, *, body: bytes, start_seq: int, end_seq: int, frame_count: int,
                    reason: int) -> dict | None:
        from blackbox.tape import seal as seal_mod
        prev = self._segment_prev_chain(n)
        return seal_mod.write(segments.seal_path(self.tape_dir, n), segment_no=n, reason=reason,
                              start_seq=start_seq, end_seq=end_seq, frame_count=frame_count,
                              body=body, prev_chain=prev, sealer=self.sealer_token)

    def _segment_range(self, n: int) -> tuple[int, int, int]:
        """(start_seq, end_seq, frame_count) for segment n, derived from its frames."""
        from blackbox.tape import frame as fm
        path = segments.seg_path(self.tape_dir, n)
        with open(path, "rb") as fh:
            buf = fh.read(self.segment_bytes)
        cur = C.SEGMENT_HEADER_BYTES
        end = self._slot.head_off if (self._slot and self._slot.active_segment == n) else len(buf)
        seqs: list[int] = []
        while cur + C.FRAME_OVERHEAD_BYTES <= end:
            try:
                fields, nxt = fm.decode(buf, cur, algo=self.checksum_algo)
            except fm.FrameError:
                break
            seqs.append(fields["seq"])
            cur = nxt
        if not seqs:
            return 0, 0, 0
        return seqs[0], seqs[-1], len(seqs)

    # ── close ───────────────────────────────────────────────────────────────
    def close(self, *, reason: int = C.SEAL_REASON_SHUTDOWN) -> None:
        if self._slot is None:
            return
        if self._batch:
            self.publish()
        n = self._slot.active_segment
        blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0,
                                algo=self.checksum_algo)
        if self._mm is not None and self._slot.head_off + len(blob) <= self.segment_bytes:
            off = self._slot.head_off
            self._mm[off:off + len(blob)] = blob
            self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
        self._publish()
        body = bytes(self._mm[C.SEGMENT_HEADER_BYTES:self._slot.head_off]) if self._mm else b""
        start_seq, end_seq, frame_count = self._segment_range(n)
        if self._mm is not None:
            self._mm.flush()
            self._mm.close()
            self._mm = None
        if self._seg_fd is not None:
            os.close(self._seg_fd)
            self._seg_fd = None
        self._write_seal(n, body=body, start_seq=start_seq, end_seq=end_seq,
                         frame_count=frame_count, reason=reason)

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def head(self) -> head_mod.HeadSlot:
        assert self._slot is not None
        return self._slot

    def stats(self) -> dict:
        return {
            "writer_uuid": self.writer_uuid.hex(), "tape_dir": self.tape_dir,
            "head_seq": self._slot.head_seq if self._slot else 0,
            "head_off": self._slot.head_off if self._slot else 0,
            "active_segment": self._slot.active_segment if self._slot else 0,
            "gen": self._slot.gen if self._slot else 0,
            "mode": self.mode, "segment_bytes": self.segment_bytes,
            "degraded": self.degraded_reason, "chain": self._chain.value.hex(),
        }

    def __enter__(self) -> "TapeWriter":
        return self.open() if self._slot is None else self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(reason=C.SEAL_REASON_SHUTDOWN if exc_type is None else C.SEAL_REASON_RUN_END)
