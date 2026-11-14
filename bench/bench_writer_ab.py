"""P1-19 — A/B: mmap'd active segment vs `write()`-append, identical framing, batching and head protocol.

The question the plan asks: if a plain `write()`-append writer is within 20 % of the mmap writer at the target
event rates, ADR-001 is reopened in favour of the simpler semantics (no mapping lifetime, no SIGBUS class, no
`FlushViewOfFile` path). Both arms use the same frame encoder, the same 16 KiB group-commit bound, the same
CHAIN frame and the same two-slot head publish; only the store differs — mmap slice assignment vs `pwrite` —
and the batch hash is taken from the frame bytes in both (from the mapping in one arm, from the bytes just
encoded in the other; neither concatenates the whole batch).

Rates: 1 000, 10 000 and 40 000 events/s offered, paced on the deadline. Per arm and rate: p50/p95/p99 of the
per-event cost, CPU time per event (`time.process_time`), and the achieved rate.

Usage: PYTHONPATH=src python bench/bench_writer_ab.py [--seconds 3] [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import _fs
from bench_frame import hardware

from blackbox import constants as C
from blackbox.codec import cbor
from blackbox.codec.ids import blake2b
from blackbox.tape import chain as chain_mod
from blackbox.tape import frame as frame_mod
from blackbox.tape import head as head_mod
from blackbox.tape import segments
from blackbox.tape.writer import TapeWriter

RATES = (1_000, 10_000, 40_000)


class WriteAppendTape:
    """`write()`-append twin of `TapeWriter`: same bytes, same head protocol, `pwrite` instead of a mapping."""

    def __init__(self, data_dir: str, writer_uuid: bytes, segment_bytes: int) -> None:
        self.tape_dir = os.path.join(data_dir, "tape", writer_uuid.hex())
        self.segment_bytes = segment_bytes
        self.fd = os.open(segments.seg_path(self.tape_dir, 1), os.O_RDWR)
        hdr = segments.decode_header(os.pread(self.fd, C.SEGMENT_HEADER_BYTES, 0))
        self.chain = chain_mod.ChainState(bytes(hdr["prev_chain"]))
        self.head_map = head_mod.HeadMap(os.path.join(self.tape_dir, "head"))
        slot = self.head_map.best()
        assert slot is not None
        self.slot = slot
        self.frame_buf = bytearray(C.FRAME_OVERHEAD_BYTES + 8192)
        self.chbuf = bytearray(256)
        self.parts: list[bytes] = []
        self.batch_first = 0
        self.pending_bytes = 0
        self.batch_bytes = C.DEFAULT_BATCH_BYTES
        self.publishes = 0

    def emit(self, kind: int, payload: bytes) -> int:
        n = frame_mod.encode_into(self.frame_buf, 0, self.slot.head_seq + 1, kind, payload, priority=1)
        frame_bytes = bytes(memoryview(self.frame_buf)[:n])
        off = self.slot.head_off
        os.pwrite(self.fd, frame_bytes, off)
        self.slot = self.slot.with_(head_seq=self.slot.head_seq + 1, head_off=off + n)
        if not self.parts:
            self.batch_first = self.slot.head_seq
        self.parts.append(frame_bytes)
        self.pending_bytes += n
        if self.pending_bytes >= self.batch_bytes:
            self.publish()
        return self.slot.head_seq

    def publish(self) -> None:
        if self.parts:
            h = hashlib.blake2b(digest_size=32)
            for part in self.parts:
                h.update(part)
            digest = h.digest()
            m = cbor.encode_chain_into(self.chbuf, 0, self.chain.value, digest,
                                       self.batch_first, self.slot.head_seq)
            size = frame_mod.encode_into(self.frame_buf, 0, self.slot.head_seq + 1, C.KIND_CHAIN,
                                         bytes(memoryview(self.chbuf)[:m]), priority=0)
            off = self.slot.head_off
            os.pwrite(self.fd, memoryview(self.frame_buf)[:size], off)
            self.slot = self.slot.with_(head_seq=self.slot.head_seq + 1, head_off=off + size)
            self.chain.value = chain_mod.chain_value(self.chain.value, digest)
        self.slot = self.head_map.publish(self.slot)
        self.publishes += 1
        self.parts = []
        self.batch_first = 0
        self.pending_bytes = 0

    def flush(self) -> None:
        if self.parts:
            self.publish()

    def close(self) -> None:
        self.flush()
        os.fsync(self.fd)
        os.close(self.fd)
        self.head_map.close()


def _mk_tape(column: dict) -> str:
    """Create the tape with the real writer, then release the mapping so the write arm can take over."""
    tmp = _fs.scratch(column, "bbx-ab-")
    w = TapeWriter(tmp, segment_bytes=256 << 20, mode="process").open()
    uuid = w.writer_uuid
    w.abort()
    return tmp, uuid


def _paced_block(emit, rate: int, seconds: float) -> dict:
    """One paced block: call `emit` on a deadline for `seconds`, return the raw samples and CPU time."""
    lat: list[int] = []
    n = 0
    cpu0 = time.process_time()
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        target = time.perf_counter() + 1.0 / rate
        t0 = time.perf_counter_ns()
        emit()
        lat.append(time.perf_counter_ns() - t0)
        n += 1
        while time.perf_counter() < target:
            time.sleep(0)
    return {"lat": lat, "events": n, "cpu": time.process_time() - cpu0}


def _summarise(blocks: list[dict], rate: int, seconds: float) -> dict:
    lat = [x for b in blocks for x in b["lat"]]
    n = len(lat)
    cpu = sum(b["cpu"] for b in blocks)
    q = statistics.quantiles(lat, n=100)
    return {"offered_ev_s": rate, "events": n, "achieved_ev_s": round(n / seconds, 1),
            "p50_us": round(q[49] / 1000, 3), "p95_us": round(q[94] / 1000, 3),
            "p99_us": round(q[98] / 1000, 3),
            "cpu_us_per_event": round(cpu / n * 1e6, 3) if n else None,
            "blocks": len(blocks)}


def measure(column: dict, seconds: float) -> dict:
    """Interleaved A/B (Stage-2 item 8): the two arms alternate in blocks, so drift hits both equally.

    Order per rate is mmap, write, write, mmap — each block is `seconds / 4` long, and both arms' tapes are
    created before the first block so that neither pays creation cost inside a measurement window.
    """
    payload = cbor.encode({"p": os.name, "m": "gpt-4o-mini", "ph": blake2b(b"x" * 1024),
                           "st": 1, "te": 2, "k": blake2b(b"y")})
    block_s = seconds / 4
    arms: dict[str, list[dict]] = {"mmap": [], "write": []}
    for rate in RATES:
        tmp_mmap, uuid = _mk_tape(column)
        w = TapeWriter(tmp_mmap, writer_uuid=uuid, segment_bytes=256 << 20, mode="process").open()
        w.batch_bytes = C.DEFAULT_BATCH_BYTES
        tmp_write, uuid2 = _mk_tape(column)
        t = WriteAppendTape(tmp_write, uuid2, 256 << 20)

        blocks: dict[str, list[dict]] = {"mmap": [], "write": []}
        for arm in ("mmap", "write", "write", "mmap"):
            if arm == "mmap":
                blocks["mmap"].append(_paced_block(
                    lambda writer=w: writer.emit(C.KIND_MODEL_REQUEST, payload), rate, block_s))
            else:
                blocks["write"].append(_paced_block(
                    lambda tape=t: tape.emit(C.KIND_MODEL_REQUEST, payload), rate, block_s))
        w.flush()
        w.close()
        _fs.release(tmp_mmap)
        t.close()
        _fs.release(tmp_write)
        arms["mmap"].append(_summarise(blocks["mmap"], rate, seconds / 2))
        arms["write"].append(_summarise(blocks["write"], rate, seconds / 2))
    ratios = []
    for a, b in zip(arms["mmap"], arms["write"], strict=True):
        ratios.append({"offered_ev_s": a["offered_ev_s"],
                       "p50_ratio": round(b["p50_us"] / a["p50_us"], 3),
                       "p99_ratio": round(b["p99_us"] / a["p99_us"], 3),
                       "cpu_ratio": round(b["cpu_us_per_event"] / a["cpu_us_per_event"], 3)})
    return {"filesystem": column["name"], "arms": arms, "ratios": ratios,
            "interleaving": "mmap, write, write, mmap per rate; block = seconds/4"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    columns = _fs.columns()
    out = {"date": time.strftime("%Y-%m-%d"), "probe": "P1-19 mmap vs write()-append (ADR-001 verdict)",
           "protocol": ("identical frame encoder, 16 KiB group commit, CHAIN frame and two-slot head publish; "
                        "only the store differs; INTERLEAVED blocks (mmap, write, write, mmap) at each offered rate"),
           "gate_column": _fs.GATE_FS, "hardware": hardware(),
           "threshold": "write() within 20 % of mmap at every rate -> ADR-001 reopened",
           "columns": {c["name"]: {"path": _fs.label(c["dir"]), "gate": c["gate"], "note": c["note"]} for c in columns},
           "results": {c["name"]: measure(c, args.seconds) for c in columns}}
    worst = max(r["p50_ratio"] for r in out["results"][_fs.GATE_FS]["ratios"])
    out["verdict"] = {
        "worst_p50_ratio": worst,
        "within_20_percent": worst <= 1.20,
        "decision": ("reopen ADR-001: write()-append is within 20 % of mmap" if worst <= 1.20
                     else "keep mmap: write()-append is more than 20 % slower at some target rate"),
    }
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-writer-ab.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    for fs_name, block in out["results"].items():
        for arm, rows in block["arms"].items():
            for r in rows:
                print(f"  [{fs_name:6s}] {arm:5s} {r['offered_ev_s']:6d} ev/s p50={r['p50_us']:8.3f} "
                      f"p99={r['p99_us']:8.3f} cpu={r['cpu_us_per_event']:8.3f} us/ev "
                      f"achieved={r['achieved_ev_s']:>8.0f}")
        for r in block["ratios"]:
            print(f"  [{fs_name:6s}] ratio at {r['offered_ev_s']:6d} ev/s: p50 x{r['p50_ratio']} "
                  f"p99 x{r['p99_ratio']} cpu x{r['cpu_ratio']}")
    print(f"  verdict: {out['verdict']['decision']} (worst p50 ratio {worst})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
