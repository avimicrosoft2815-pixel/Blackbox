"""The hot path: encode a batch of frames, write it with one `pwrite`, publish the two-slot head (P1-08).

The store is `pwrite`-append, not a mapping (ADR-005, C17). Frames are encoded with `pack_into` into one
reusable batch buffer, the whole batch reaches the segment in **one syscall**, and the commit is the head
store. A mapping would buy nothing here and costs three things the plan cannot have: a page-fault class the
recorder cannot refuse (`SIGBUS` on a truncated segment), `FlushViewOfFile` as a second flush path on
Windows, and a mapped file that cannot be deleted or rolled while the mapping lives.

Order of operations on every publish is the linearization point: the batch `pwrite` completes before the
head slot is published, and readers only trust frames below the published `head_off`.

Guarantees this file is allowed to make (plan §10):
  * `mode=process` → **F1** (process death) for committed frames, with **one syscall per batch**, not per event.
  * `mode=kernel`/`paranoid` → additionally group-commit `fdatasync` for F2.
It never claims F3, and never raises into user code: allocation failure degrades (P1-20).
"""
from __future__ import annotations

import os
import threading
import time

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.codec.ids import new_writer_uuid
from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.tape import chain as chain_mod
from blackbox.tape import frame as frame_mod
from blackbox.tape import head as head_mod
from blackbox.tape import meta as meta_mod
from blackbox.tape import prealloc, segments

DIE_ENV = "BLACKBOX_DIE_AT"
_plat = _plat_current()          # pwrite/pread, because Windows has neither (C22)


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
                 chain_enabled: bool = True, sealer_token: bytes = b"",
                 sync_ms: int = C.DEFAULT_SYNC_MS, sync_bytes: int = C.DEFAULT_SYNC_BYTES,
                 publish_ms: float = C.DEFAULT_PUBLISH_MS) -> None:
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
        self._seg_fd: int | None = None
        # The batch buffer: frames are encoded into it with `pack_into` and leave for the segment in one
        # `pwrite` per batch. `_buf_base` is the segment offset of `_buf[0]`, so any frame's position in the
        # segment is `_buf_base + <offset in buffer>`.
        self._buf = bytearray(C.DEFAULT_BATCH_BYTES + C.FRAME_MAX_BYTES)
        self._buf_base = C.SEGMENT_HEADER_BYTES
        self._fill = 0
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
        self.batch_ms = publish_ms                # upper bound on the group-commit window (C3)
        self.sync_ms = sync_ms                    # kernel/paranoid fdatasync interval
        self.sync_bytes = sync_bytes
        # group-commit observations (C3): the widest window actually left unpublished, in ms
        self.window_max_ms = 0.0
        self._window_start_ns = 0
        self.publishes = 0
        self.syncs = 0
        self.dropped_events = 0
        # The keeper thread publishes on a tick while the app thread appends. Roll mutates the mapping,
        # the head and the chain, so publish/flush/roll/close are serialised; append keeps its own short
        # critical section so a publish never sees a half-recorded batch.
        self._lock = threading.RLock()
        self.degraded_reason: str | None = None
        self.recording = True                 # False = degraded sink (no segment could be allocated)
        self.adopted: dict[str, object] | None = None   # open() completed a roll or rebuilt the head

    # ── lifecycle ───────────────────────────────────────────────────────────
    def arm_die(self, stage: str | None = None) -> str | None:
        """Test hook (P2-08): arm a named crash point; `None` reads `BLACKBOX_DIE_AT`."""
        return arm_die(stage)

    def open(self) -> TapeWriter:
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
        self._head.half_hook = lambda: _die("S4")   # type: ignore[assignment]  # only used when armed
        best = self._head.best()
        if best is None and existing:
            # Both slots unreadable but frames exist: the head is a hint, so rebuild it from the segments
            # instead of refusing a legal crash state (kill matrix seed 120).
            from blackbox.tape import repair as repair_mod

            report = repair_mod.rebuild_head(self.tape_dir, sealer=self.sealer_token)
            if report is not None:
                self.adopted = report
                best = self._head.best()
        if best is None:
            if existing:
                raise TapeError("segments exist but the head is unreadable; run recovery")
            self._slot = head_mod.HeadSlot(head_off=C.SEGMENT_HEADER_BYTES, active_segment=1)
            try:
                self._create_segment(1)
            except prealloc.PreallocError:
                # S17 covers the first segment too, not just a roll: a recorder that cannot get its first
                # segment must degrade into a sink, never raise into the user's program (§8.6 step 2,
                # "never raise into the app"). Nothing is published, so no reader ever sees a head that
                # names a segment which does not exist.
                self.degraded_reason = "NO_SPACE"
                self.recording = False
                self._slot = None
                return self
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
        assert self._head is not None
        self._head.reload()
        self.adopted = report
        return True

    def _read_meta(self) -> bytes:
        with open(self.meta_path, "rb") as fh:
            return bytes(fh.read(C.META_BYTES))

    def _segment_prev_chain(self, n: int) -> bytes:
        with open(segments.seg_path(self.tape_dir, n), "rb") as fh:
            hdr = segments.decode_header(fh.read(C.SEGMENT_HEADER_BYTES))
        return bytes(hdr["prev_chain"])

    def _create_segment(self, n: int) -> None:
        prev = self._chain.value
        segments.create(self.tape_dir, n, writer_uuid=self.writer_uuid,
                        segment_bytes=self.segment_bytes, prev_chain=prev)

    def _map_segment(self, n: int) -> None:
        """Attach to segment n. The name is historical: there is no mapping any more, just the fd and a buffer."""
        self._flush_buf()
        if self._seg_fd is not None:
            os.close(self._seg_fd)
        self._seg_path = segments.seg_path(self.tape_dir, n)
        self._seg_fd = _plat.open_fd(self._seg_path, os.O_RDWR)
        # A reopened writer continues where the head says, not at the top of the segment.
        self._buf_base = (self._slot.head_off
                          if self._slot is not None and self._slot.active_segment == n
                          else C.SEGMENT_HEADER_BYTES)
        self._fill = 0

    # ── the store ───────────────────────────────────────────────────────────
    def _flush_buf(self) -> None:
        """Hand the encoded batch to the kernel: ONE `pwrite`, and it is the only data syscall on the path."""
        if self._fill and self._seg_fd is not None:
            _plat.pwrite(self._seg_fd, memoryview(self._buf)[:self._fill], self._buf_base)
            self._buf_base += self._fill
        self._fill = 0

    def _reserve(self, size: int) -> int:
        """Make room for `size` bytes at the end of the buffer and return the offset inside it."""
        if self._fill + size > len(self._buf):
            self._flush_buf()
            if size > len(self._buf):
                self._buf = bytearray(size)
        return self._fill    # `_buf_base + this` is the segment offset

    # ── append / publish ────────────────────────────────────────────────────
    def append(self, kind: int, payload: bytes = b"", *, priority: int = 1, flags: int = 0,
               run_slot: int = 0, node_slot: int = 0, wall_ns: int | None = None,
               mono_ns: int | None = None) -> int:
        with self._lock:
            return self._append_locked(kind, payload, priority=priority, flags=flags, run_slot=run_slot,
                                       node_slot=node_slot, wall_ns=wall_ns, mono_ns=mono_ns)

    def _append_locked(self, kind, payload, *, priority, flags, run_slot, node_slot, wall_ns, mono_ns) -> int:
        if not self.recording:
            self.dropped_events += 1        # sink: counted, never raised (S17)
            return 0
        assert self._slot is not None and self._seg_fd is not None
        size = frame_mod.frame_size(len(payload))
        if size > self.segment_bytes - C.SEGMENT_HEADER_BYTES - C.RESERVED_HEAD_ROOM:
            raise bbx("BBX-E225", f"frame of {size} B does not fit a {self.segment_bytes} B segment")
        if (self._slot.head_off + size + C.RESERVED_HEAD_ROOM > self.segment_bytes
                and not self.roll()):
            # Degraded and no room for another segment: drop the frame and count it. The app never sees an
            # exception from the recorder (C5 fail-open); readers see the DEGRADED frame and the count.
            self.dropped_events += 1
            self._slot = self._slot.with_(dropped_events=self._slot.dropped_events + 1)
            return self._slot.head_seq

        seq = self._slot.head_seq + 1
        off = self._slot.head_off
        at = self._reserve(size)
        assert self._buf_base + at == off, "the buffer and the head must agree where the next frame goes"
        hook = _die if _ARMED else None
        size = frame_mod.encode_into(self._buf, at, seq, kind, payload, flags=flags, priority=priority,
                                    run_slot=run_slot, node_slot=node_slot, wall_ns=wall_ns,
                                    mono_ns=mono_ns, hook=hook)
        self._fill = at + size
        _die("S3")

        if not self._batch_spans:
            self._batch_first_seq = seq
        self._batch_spans.append((at, size))
        self._frame_count += 1
        self._pending += 1
        self._pending_bytes += size
        self._unpublished_bytes += size
        self._slot.head_seq = seq
        self._slot.head_off = off + size
        return seq

    def publish(self, *, publish_chain: bool = True) -> head_mod.HeadSlot | None:
        """Publish the current head. Also commits a CHAIN frame for the batch when enabled."""
        with self._lock:
            if not self.recording:
                return None
            assert self._slot is not None
            if publish_chain and self.chain_enabled and self._batch_spans:
                batch_hash = chain_mod.batch_hash_spans(self._buf, self._batch_spans)
                # Decide the roll *before* the CHAIN frame is written: the frame carries `last_seq`, and a roll
                # advances `head_seq` (the SEG_END frame), so writing first would duplicate a seq number. The bound
                # is the largest a CHAIN frame can be (32+32 byte hashes plus three u64s), so no probe encode.
                need = chain_mod.MAX_CHAIN_FRAME_BYTES + C.RESERVED_HEAD_ROOM
                if self._slot.head_off + need > self.segment_bytes:
                    self.roll()                   # returns False when the roll degraded; checked below
                if self._slot.head_off + need <= self.segment_bytes:
                    n = cbor.encode_chain_into(self._scratch, 0, self._chain.value, batch_hash,
                                               self._batch_first_seq, self._slot.head_seq)
                    off = self._slot.head_off
                    at = self._fill
                    assert self._buf_base + at == off
                    written = frame_mod.encode_into(self._buf, at, self._slot.head_seq + 1, C.KIND_CHAIN,
                                                    bytes(memoryview(self._scratch)[:n]), priority=0)
                    self._fill = at + written
                    self._slot.head_seq += 1
                    self._slot.head_off = off + written
                    self._frame_count += 1
                    self._chain.value = chain_mod.chain_value(self._chain.value, batch_hash)
            self._flush_buf()                  # data first ...
            self._slot = self._publish()       # ... then the commit, which is the linearization point

            if self.mode in ("kernel", "paranoid"):
                self._maybe_sync(force=(self.mode == "paranoid"))
            self.publishes += 1
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
        self._head.publish(self._slot, split=_ARMED)   # gen += 1 and one 64 B pwrite
        _die("S5")
        return self._slot

    def _maybe_sync(self, *, force: bool = False) -> None:
        if self._seg_fd is None:
            return
        now = time.perf_counter_ns()
        due = (now - self._last_sync_ns) >= self.sync_ms * 1_000_000 or self._unpublished_bytes >= self.sync_bytes
        if not (force or due):
            return
        self._flush_buf()
        if hasattr(os, "fdatasync"):
            os.fdatasync(self._seg_fd)
        else:
            os.fsync(self._seg_fd)
        if self._head is not None:
            self._head.sync()
        self._last_sync_ns = now
        self._unpublished_bytes = 0
        self.syncs += 1

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
        due_priority = kind in C.PRIORITY_KINDS
        if due_bytes or due_ms or due_priority:
            self.publish()
        else:
            window_ms = (now - self._last_publish_ns) / 1_000_000
            self.window_max_ms = max(self.window_max_ms, window_ms)      # a measured bound, not a promise
        return seq

    def publish_if_due(self, now_ns: int | None = None) -> bool:
        """Called by the keeper: publish only if the window bound or the byte bound has expired.

        Returns True if a publish happened. Never raises for "nothing to do" — the keeper must stay cheap.
        """
        with self._lock:
            if not self.recording:
                return False
            if not self._pending:
                return False
            now = time.perf_counter_ns() if now_ns is None else now_ns
            due_ms = self.batch_ms and (now - self._last_publish_ns) >= int(self.batch_ms * 1_000_000)
            due_bytes = self.batch_bytes and self._pending_bytes >= self.batch_bytes
            if due_ms or due_bytes:
                self.publish()
                return True
            window_ms = (now - self._last_publish_ns) / 1_000_000
            self.window_max_ms = max(self.window_max_ms, window_ms)
            return False

    @property
    def chain_value(self) -> bytes:
        """The current chain head, so a caller that writes a `GAP` can carry the link (F3/D-017)."""
        return self._chain.value

    def set_tail_seq(self, seq: int) -> None:
        """Advance `tail_seq` (the oldest seq a reader may still expect) after retention deleted a range."""
        with self._lock:
            if not self.recording or self._slot is None:
                return
            if seq <= self._slot.tail_seq:
                return
            self._slot = self._slot.with_(tail_seq=seq)
            self._publish()

    def window_ms(self) -> float:
        """Age of the oldest unpublished frame, in ms (C3 evidence)."""
        if not self._pending:
            return 0.0
        return (time.perf_counter_ns() - self._last_publish_ns) / 1_000_000

    def flush(self) -> head_mod.HeadSlot | None:
        """Publish whatever is pending (and sync in kernel/paranoid). None when degraded to a sink."""
        with self._lock:
            if not self.recording:
                return None
            assert self._slot is not None
            if self._batch_spans or self._slot.gen == 0:
                return self.publish()
            return self._slot

    # ── roll ────────────────────────────────────────────────────────────────
    def roll(self) -> bool:
        """Close the active segment and start the next one (P1-09, §8.6).

        Returns True when the roll happened. A preallocation failure (ENOSPC/EDQUOT) does **not** raise into
        the app: the writer stays on the current segment, sets DEGRADED_NO_SPACE in the head, and keeps
        recording until the segment is full (C5, S17). Returns False in that case.
        """
        with self._lock:
            if not self.recording:
                return False               # a sink has nothing to roll
            arm_die()                      # roll is rare; re-reading the env here keeps S14/S15 armable
            assert self._slot is not None and self._seg_fd is not None
            n = self._slot.active_segment
            # 1. SEG_END commits the current head
            blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0)
            if self._slot.head_off + len(blob) <= self.segment_bytes:
                off = self._slot.head_off
                at = self._reserve(len(blob))
                assert self._buf_base + at == off
                self._buf[at:at + len(blob)] = blob
                self._fill = at + len(blob)
                self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
            self._flush_buf()
            self._publish()
            _die("S14")     # after SEG_END published, before the next segment is durable

            # 2. reserve space for n+1 *before* closing n down: if the reservation fails, the active segment
            # stays open and appendable, so the app keeps recording (S17/C5). The header is written later,
            # because it chains to the seal of n, which does not exist yet.
            next_no = n + 1
            try:
                reserved = segments.reserve(self.tape_dir, next_no, segment_bytes=self.segment_bytes)
            except prealloc.PreallocError:
                self.degraded_reason = "NO_SPACE"
                self._slot = self._slot.with_(flags=self._slot.flags | C.HEAD_FLAG_DEGRADED_NO_SPACE)
                self._publish()
                self._emit_degraded("NO_SPACE")
                return False

            # 3. capture the body of segment n and seal it (footer sidecar; P1-13)
            self._flush_buf()
            body = self._read_body(n)
            start_seq, end_seq, frame_count = self._segment_range(n)
            if self._seg_fd is not None:
                os.close(self._seg_fd)
                self._seg_fd = None
            seal_info = self._write_seal(n, body=body, start_seq=start_seq, end_seq=end_seq,
                                         frame_count=frame_count, reason=C.SEAL_REASON_ROLLED)
            if seal_info is not None:
                self._chain.value = seal_info["chain"]     # the next segment's header chains to this seal

            # 4. header the reserved segment and publish it into place
            segments.finish(self.tape_dir, next_no, reserved, writer_uuid=self.writer_uuid,
                            segment_bytes=self.segment_bytes, prev_chain=self._chain.value)
            _die("S14b")
            self._map_segment(next_no)
            _die("S15")     # segment n+1 exists (and its header is durable) but the head still names n
            self._slot = self._slot.with_(active_segment=next_no, head_off=C.SEGMENT_HEADER_BYTES,
                                          flags=self._slot.flags & ~C.HEAD_FLAG_ROLL_PENDING)
            self._publish()
            return True

    def _emit_degraded(self, reason: str) -> None:
        """Append one DEGRADED frame if it fits. Failures here are swallowed: recording must not raise."""
        if self._seg_fd is None or self._slot is None:
            return
        try:
            payload = cbor.encode({"d": reason, "s": self._slot.active_segment,
                                   "drop": self._slot.dropped_events})
            blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_DEGRADED, payload, priority=0)
            if self._slot.head_off + len(blob) <= self.segment_bytes:
                off = self._slot.head_off
                at = self._reserve(len(blob))
                assert self._buf_base + at == off
                self._buf[at:at + len(blob)] = blob
                self._fill = at + len(blob)
                self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
                self._flush_buf()
                self._publish()
        except Exception:
            return

    def _read_body(self, n: int) -> bytes:
        """The sealed prefix of segment n, read back from the file (there is no mapping to slice any more)."""
        assert self._seg_fd is not None and self._slot is not None
        end = self._slot.head_off if self._slot.active_segment == n else self.segment_bytes
        body: bytes = _plat.pread(self._seg_fd, max(end - C.SEGMENT_HEADER_BYTES, 0), C.SEGMENT_HEADER_BYTES)
        return body

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
        """Flush, SEG_END, seal, release the mapping. Idempotent."""
        with self._lock:
            if self._slot is None:
                if self._head is not None:
                    self._head.close()
                    self._head = None
                return
            if self._batch_spans:
                self.publish()
            n = self._slot.active_segment
            blob = frame_mod.encode(self._slot.head_seq + 1, C.KIND_SEG_END, b"", priority=0)
            if self._seg_fd is not None and self._slot.head_off + len(blob) <= self.segment_bytes:
                off = self._slot.head_off
                at = self._reserve(len(blob))
                assert self._buf_base + at == off
                self._buf[at:at + len(blob)] = blob
                self._fill = at + len(blob)
                self._slot = self._slot.with_(head_seq=self._slot.head_seq + 1, head_off=off + len(blob))
            self._flush_buf()
            self._publish()
            body = self._read_body(n) if self._seg_fd is not None else b""
            start_seq, end_seq, frame_count = self._segment_range(n)
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
        """Drop the buffer without committing anything (used by recovery and by idempotence tests)."""
        self._fill = 0                   # nothing staged is written: abort must not create bytes
        self._buf_base = C.SEGMENT_HEADER_BYTES
        if self._seg_fd is not None:
            os.close(self._seg_fd)
            self._seg_fd = None
        if self._head is not None:
            self._head.close()
            self._head = None

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def head(self) -> head_mod.HeadSlot:
        if self._slot is None:
            raise TapeError("this writer is degraded: no segment could be allocated, so there is no head "
                            "(see .degraded_reason)")
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

    def __enter__(self) -> TapeWriter:
        return self.open() if self._slot is None else self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(reason=C.SEAL_REASON_SHUTDOWN if exc_type is None else C.SEAL_REASON_RUN_END)
