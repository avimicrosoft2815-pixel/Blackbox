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
from blackbox.codec import cbor
from blackbox.codec.ids import new_writer_uuid

DIE_ENV = "BLACKBOX_DIE_AT"


class TapeError(RuntimeError):
    pass


# Crash-point state (P2-08). `os.environ` lookups cost ~1 us each, which is ~6 us per event if done on the
# append path, so the stage is read once when the writer is armed and compared as a module global afterwards.
_STAGE: str | None = None
_ARMED = False


def arm_die(stage: str | None = None) -> str | None:
    """Arm the crash point named by `BLACKBOX_DIE_AT` (or `stage`). Tests call this just before the event
    they want to die on; production never sets the variable, so `_die` is a single compare."""
    global _STAGE, _ARMED
    _STAGE = stage if stage is not None else os.environ.get(DIE_ENV)
    _ARMED = _STAGE is not None
    return _STAGE


def _die(stage: str) -> None:
    """Crash the process at a named write stage, exactly like `kill -9` (no atexit, no flush)."""
    if _ARMED and stage == _STAGE:
        os._exit(137)


class TapeWriter:
    def __init__(self, data_dir: str, *, writer_uuid: bytes | None = None,
                 segment_bytes: int = C.DEFAULT_SEGMENT_BYTES, mode: str = C.DEFAULT_MODE,
                 vantage: int = 1,
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
        self.chain_enabled = chain_enabled
        self.sealer_token = sealer_token or b""

        self.tape_dir = os.path.join(data_dir, "tape", self.writer_uuid.hex())
        self.meta_path = os.path.join(self.tape_dir, "meta")
        self.head_path = os.path.join(self.tape_dir, "head")

        self._slot: head_mod.HeadSlot | None = None
        self._head: head_mod.HeadMap | None = None
        self._mm: mmap.mmap | None = None
        self._seg_fd: int | None = None
        self._seg_path: str | None = None
        self._chain = chain_mod.ChainState()
        self._batch_spans: list[tuple[int, int]] = []
        self._scratch = bytearray(256)      # CHAIN payload scratch, reused across publishes
        self._batch_first_seq = 0
        self._frame_count = 0
        self._last_sync_ns = time.perf_counter_ns()
        self._unpublished_bytes = 0
        self._pending = 0
        self._pending_bytes = 0        # bytes appended since the last publish (group-commit threshold)
        self._last_publish_ns = time.perf_counter_ns()
        self.batch_bytes = C.DEFAULT_BATCH_BYTES   # 0 = publish on every call (small batches by hand)
        self.batch_ms = 0.0
        self.degraded_reason: str | None = None
        self.adopted: dict | None = None      # set when open() completed an interrupted roll (S15)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def arm_die(self, stage: str | None = None) -> str | None:
        """Test hook (P2-08): arm a named crash point; `None` reads `BLACKBOX_DIE_AT`."""
        return arm_die(stage)

    def open(self) -> "TapeWriter":
        arm_die()
        os.makedirs(self.tape_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(self.meta_path):
            blob = meta_mod.encode(writer_uuid=self.writer_uuid, segment_bytes=self.segment_bytes,
                                   vantage=self.vantage)
            with open(self.meta_path, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        meta = meta_mod.decode(self._read_meta())
        self.segment_bytes = meta["segment_bytes"]

        existing = segments.list_segments(self.tape_dir)
        self._head = head_mod.HeadMap(self.head_path)
        self._head.half_hook = lambda: _die("S4")
        best = self._head.best()
        if best is None:
            if existing:
                raise TapeError("segments exist but the head is unreadable; run recovery")
            self._slot = head_mod.HeadSlot(head_off=C.SEGMENT_HEADER_BYTES, active_segment=1)
            try:
                self._create_segment(1)
            except prealloc.PreallocError as exc:
                self.degraded_reason = "NO_SPACE"
                raise bbx("BBX-E132", str(exc)) from exc
            self._map_segment(1)
            self._publish()
        else:
            # P1-09 / S15: an interrupted roll is completed, not refused (idempotent with rec/discover.py)
            if self._complete_roll_if_needed():
                best = self._head.best() or best
            self._slot = best
            n = best.active_segment
            if not os.path.exists(segments.seg_path(self.tape_dir, n)):
                raise TapeError(f"active segment {n} missing")
            self._map_segment(n)
            self._chain.value = self._segment_prev_chain(n)
            # a reopened writer must never reuse seq numbers
            self._frame_count = 0
        return self

    def _complete_roll_if_needed(self) -> bool:
        from blackbox.tape import repair

        report = repair.adopt_orphan(self.tape_dir, sealer=self.sealer_token)
        if report is None:
            return False
        self._head.reload()
        self.adopted = report
        return True

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
        off = self._slot.head_off
        hook = _die if _ARMED else None
        size = frame_mod.encode_into(self._mm, off, seq, kind, payload, flags=flags, priority=priority,
                                    run_slot=run_slot, node_slot=node_slot, wall_ns=wall_ns,
                                    mono_ns=mono_ns, hook=hook)
        _die("S3")

        if not self._batch_spans:
            self._batch_first_seq = seq
        self._batch_spans.append((off, size))
        self._frame_count += 1
        self._pending += 1
        self._pending_bytes += size
        self._unpublished_bytes += size
        self._slot.head_seq = seq
        self._slot.head_off = off + size
        return seq

    def publish(self, *, publish_chain: bool = True) -> head_mod.HeadSlot:
        """Publish the current head. Also commits a CHAIN frame for the batch when enabled."""
        assert self._slot is not None
        if publish_chain and self.chain_enabled and self._batch_spans:
            batch_hash = chain_mod.batch_hash_spans(self._mm, self._batch_spans)
            # Decide the roll *before* the CHAIN frame is written: the frame carries `last_seq`, and a roll
            # advances `head_seq` (the SEG_END frame), so writing first would duplicate a seq number. The bound
            # is the largest a CHAIN frame can be (32+32 byte hashes plus three u64s), so no probe encode.
            if self._slot.head_off + chain_mod.MAX_CHAIN_FRAME_BYTES + C.RESERVED_HEAD_ROOM > self.segment_bytes:
                self.roll()
            n = cbor.encode_chain_into(self._scratch, 0, self._chain.value, batch_hash,
                                       self._batch_first_seq, self._slot.head_seq)
            payload = memoryview(self._scratch)[:n]
            off = self._slot.head_off
            written = frame_mod.encode_into(self._mm, off, self._slot.head_seq + 1, C.KIND_CHAIN, payload,
                                           priority=0)
            self._slot.head_seq += 1
            self._slot.head_off = off + written
            self._frame_count += 1
            self._chain.value = chain_mod.chain_value(self._chain.value, batch_hash)
        self._slot = self._publish()

        if self.mode in ("kernel", "paranoid"):
            self._maybe_sync(force=(self.mode == "paranoid"))
        self._batch_spans = []
        self._batch_first_seq = 0
        self._pending = 0
        self._pending_bytes = 0        # bytes appended since the last publish (group-commit threshold)
        self._last_publish_ns = time.perf_counter_ns()
        return self._slot

    def _publish(self) -> head_mod.HeadSlot:
        """One publish = `gen += 1` and two stores into the mapped head. No syscall in process mode."""
        assert self._slot is not None and self._head is not None
        _die("S3")
        self._head.publish(self._slot)      # gen += 1 and two stores into the mapping
        _die("S5")
        return self._slot

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
        if self._head is not None:
            self._head.sync()
        self._last_sync_ns = now
        self._unpublished_bytes = 0

    def append_and_publish(self, kind: int, payload: bytes = b"", **kw) -> int:
        seq = self.append(kind, payload, **kw)
        self.publish()
        return seq

    def emit(self, kind: int, payload: bytes = b"", **kw) -> int:
        """Group commit: append now, publish when `batch_bytes` / `batch_ms` says so (or on `flush`).

        Under light load the window is one event; under load the batch grows by itself, which is why the
        commit cost per event falls instead of rising.
        """
        seq = self.append(kind, payload, **kw)
        if not self.batch_bytes and not self.batch_ms:
            self.publish()                      # one event per commit: the window is as small as it can be
            return seq
        now = time.perf_counter_ns()
        due_bytes = self.batch_bytes and self._pending_bytes >= self.batch_bytes
        due_ms = self.batch_ms and (now - self._last_publish_ns) >= int(self.batch_ms * 1_000_000)
        if due_bytes or due_ms:
            self.publish()
        return seq

    def flush(self) -> head_mod.HeadSlot:
        """Publish whatever is pending (and sync in kernel/paranoid)."""
        assert self._slot is not None
        if self._batch_spans or self._slot.gen == 0:
            return self.publish()
        return self._slot

    # ── roll ────────────────────────────────────────────────────────────────
    def roll(self) -> None:
        """Close the active segment and start the next one (P1-09, §8.6)."""
        arm_die()                      # roll is rare; re-reading the env here keeps S14/S15 armable
        assert self._slot is not None and self._mm is not None
        n = self._slot.active_segment
        # 1. SEG_END commits the current head
        blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0)
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
            raise bbx("BBX-E132", "preallocation failed at roll; recording degraded") from None
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
                fields, nxt = fm.decode(buf, cur)
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
        if self._batch_spans:
            self.publish()
        n = self._slot.active_segment
        blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0)
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
        if self._head is not None:
            if self.mode in ("kernel", "paranoid"):
                self._head.sync()
            self._head.close()
            self._head = None

    def abort(self) -> None:
        """Release mappings without committing anything (used by recovery and by idempotence tests)."""
        if self._mm is not None:
            try:
                self._mm.close()
            except (ValueError, BufferError):
                pass
            self._mm = None
        if self._seg_fd is not None:
            os.close(self._seg_fd)
            self._seg_fd = None
        if self._head is not None:
            self._head.close()
            self._head = None

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
