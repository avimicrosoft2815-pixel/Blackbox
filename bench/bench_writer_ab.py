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
import json
import mmap
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
    """`write()`-append twin of `TapeWriter`: same bytes, same head protocol, **one `pwrite` per batch**.

    C17 asked for the arm to be checked before it is measured, because a per-event `pwrite` would amortise
    nothing and make the comparison meaningless. So each batch encodes into one preallocated `bytearray`
    (`encode_into` calls `pack_into`, exactly what the mmap arm does into the mapping), the batch is written
    with a single `pwrite` at the batch's `head_off`, the CHAIN frame with a second, and the head with the same
    `HeadMap` publish the mmap arm uses. Per-event Python work is therefore: one CBOR encode, one frame encode
    into a buffer, one span append.
    """

    CAPACITY = C.DEFAULT_BATCH_BYTES + C.FRAME_MAX_BYTES          # one batch + the largest frame

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
        # Plain ints on the hot path: building a HeadSlot per event costs ~0.5 us and is exactly the kind of
        # extra per-event work C17 said to remove before measuring. The slot object is built once per publish.
        self.seq = slot.head_seq
        self.off = slot.head_off
        self.batch = bytearray(self.CAPACITY)
        self.chbuf = bytearray(256)
        self.fill = 0                     # bytes encoded into `self.batch`
        self.spans: list[tuple[int, int]] = []
        self.batch_first = 0
        self.pending_bytes = 0
        self.batch_bytes = C.DEFAULT_BATCH_BYTES
        self.syscalls = 0                 # pwrite calls, so the bench can show the arm is not per-event

    def emit(self, kind: int, payload: bytes) -> int:
        seq = self.seq + 1
        n = frame_mod.encode_into(self.batch, self.fill, seq, kind, payload, priority=1)
        if not self.spans:
            self.batch_first = seq
        self.spans.append((self.fill, n))
        self.fill += n
        self.pending_bytes += n
        self.seq = seq
        self.off += n
        if self.pending_bytes >= self.batch_bytes or self.fill + C.FRAME_MAX_BYTES > self.CAPACITY:
            self.publish()
        return seq

    def publish(self) -> None:
        if self.spans:
            base = self.off - self.fill                     # where this batch starts in the segment
            os.pwrite(self.fd, memoryview(self.batch)[:self.fill], base)     # ONE syscall per batch
            digest = chain_mod.batch_hash_spans(self.batch, self.spans)
            m = cbor.encode_chain_into(self.chbuf, 0, self.chain.value, digest,
                                       self.batch_first, self.seq)
            size = frame_mod.encode_into(self.batch, self.fill, self.seq + 1, C.KIND_CHAIN,
                                         bytes(memoryview(self.chbuf)[:m]), priority=0)
            os.pwrite(self.fd, memoryview(self.batch)[self.fill:self.fill + size],
                      self.off)                              # second syscall: the CHAIN frame
            self.seq += 1
            self.off += size
            self.chain.value = chain_mod.chain_value(self.chain.value, digest)
            self.syscalls += 1 + 1
        self.slot = self.slot.with_(head_seq=self.seq, head_off=self.off)
        self.slot = self.head_map.publish(self.slot)          # head slot: the same publish as the mmap arm
        self.spans = []
        self.fill = 0
        self.batch_first = 0
        self.pending_bytes = 0

    def flush(self) -> None:
        if self.spans:
            self.publish()

    def close(self) -> None:
        self.flush()
        os.fsync(self.fd)
        os.close(self.fd)
        self.head_map.close()


class MmapAppendTape:
    """Reference arm: the writer as it was before ADR-005 — frames encoded straight into the mapping.

    Kept here, not in `src/`, so the A/B that decided ADR-005 can be re-run and audited. It is the same
    protocol as `TapeWriter` used to be: `pack_into` into the MAP_SHARED segment at `head_off`, batch hash
    spans read back off the mapping, and the head published through `HeadMap`.
    """

    def __init__(self, data_dir: str, writer_uuid: bytes, segment_bytes: int) -> None:
        self.tape_dir = os.path.join(data_dir, "tape", writer_uuid.hex())
        self.segment_bytes = segment_bytes
        self.fd = os.open(segments.seg_path(self.tape_dir, 1), os.O_RDWR)
        self.mm = mmap.mmap(self.fd, segment_bytes)
        hdr = segments.decode_header(os.pread(self.fd, C.SEGMENT_HEADER_BYTES, 0))
        self.chain = chain_mod.ChainState(bytes(hdr["prev_chain"]))
        self.head_map = head_mod.HeadMap(os.path.join(self.tape_dir, "head"))
        slot = self.head_map.best()
        assert slot is not None
        self.slot = slot
        self.seq = slot.head_seq
        self.off = slot.head_off
        self.spans: list[tuple[int, int]] = []
        self.batch_first = 0
        self.pending_bytes = 0
        self.batch_bytes = C.DEFAULT_BATCH_BYTES
        self.syscalls = 0

    def emit(self, kind: int, payload: bytes) -> int:
        seq = self.seq + 1
        n = frame_mod.encode_into(self.mm, self.off, seq, kind, payload, priority=1)
        if not self.spans:
            self.batch_first = seq
        self.spans.append((self.off, n))
        self.off += n
        self.seq = seq
        self.pending_bytes += n
        if self.pending_bytes >= self.batch_bytes:
            self.publish()
        return seq

    def publish(self) -> None:
        if self.spans:
            digest = chain_mod.batch_hash_spans(self.mm, self.spans)
            scratch = bytearray(256)
            m = cbor.encode_chain_into(scratch, 0, self.chain.value, digest, self.batch_first, self.seq)
            size = frame_mod.encode_into(self.mm, self.off, self.seq + 1, C.KIND_CHAIN,
                                         bytes(memoryview(scratch)[:m]), priority=0)
            self.off += size
            self.seq += 1
            self.chain.value = chain_mod.chain_value(self.chain.value, digest)
        self.slot = self.slot.with_(head_seq=self.seq, head_off=self.off)
        self.slot = self.head_map.publish(self.slot)
        self.spans = []
        self.batch_first = 0
        self.pending_bytes = 0

    def flush(self) -> None:
        if self.spans:
            self.publish()

    def close(self) -> None:
        self.flush()
        self.mm.flush()
        self.mm.close()
        os.fsync(self.fd)
        os.close(self.fd)
        self.head_map.close()


def _mk_tape(column: dict, segment_bytes: int = 256 << 20) -> str:
    """Create the tape with the real writer, then release it so both reference arms can take over.

    The tape on disk is produced by `TapeWriter` (the production write()-append writer), so both arms below are
    writing through a header, a head file and a chain state that the real writer created.
    """
    tmp = _fs.scratch(column, "bbx-ab-")
    w = TapeWriter(tmp, segment_bytes=segment_bytes, mode="process").open()
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


def profile_arms(column: dict, n: int = 200_000) -> dict:
    """C17 step 1: per-event Python overhead of both arms, with the publish path amortised away.

    Unpaced, one huge batch (no `pwrite`, no mapping traffic beyond the frame writes), N events each. If the
    arms differ by more than ~0.2 us/event, the slower arm is carrying an extra per-event copy or syscall and
    must be fixed before the comparison means anything.
    """
    payload = cbor.encode({"p": os.name, "m": "gpt-4o-mini", "ph": blake2b(b"x" * 1024),
                           "st": 1, "te": 2, "k": blake2b(b"y")})
    tmp_mmap, uuid = _mk_tape(column, 512 << 20)
    w = MmapAppendTape(tmp_mmap, uuid, 512 << 20)
    tmp_write, uuid2 = _mk_tape(column, 512 << 20)
    t = WriteAppendTape(tmp_write, uuid2, 512 << 20)
    t.batch_bytes = 1 << 40                     # never publish inside the loop
    out = {}
    for name, emit in (("mmap", lambda: w.emit(C.KIND_MODEL_REQUEST, payload)),
                       ("write", lambda: t.emit(C.KIND_MODEL_REQUEST, payload))):
        lat = []
        for _ in range(2_000):                  # warm-up
            emit()
        t0 = time.process_time()
        for _ in range(n):
            s0 = time.perf_counter_ns()
            emit()
            lat.append(time.perf_counter_ns() - s0)
        cpu = time.process_time() - t0
        q = statistics.quantiles(lat, n=100)
        out[name] = {"n": n, "p50_ns": round(q[49], 1), "mean_ns": round(statistics.fmean(lat), 1),
                     "cpu_ns_per_event": round(cpu / n * 1e9, 1), "pwrite_calls": t.syscalls}
    tmp_syscalls = out["write"]["pwrite_calls"]
    out["difference_ns_per_event"] = round(out["write"]["cpu_ns_per_event"] - out["mmap"]["cpu_ns_per_event"], 1)
    out["note"] = ("one huge batch: the publish path is amortised away, so this measures per-event Python only; "
                   f"the write arm issued {tmp_syscalls} pwrite calls for {n} events (expected 0)")
    w.close()
    _fs.release(tmp_mmap)
    t.close()
    _fs.release(tmp_write)
    return out


def measure(column: dict, seconds: float, pairs: int) -> dict:
    """C17 step 2: `pairs` interleaved A/B pairs per rate, order alternating (A,B / B,A / A,B ...).

    Each block is `seconds` long. The p50 ratio is computed per pair, so the reported median/min/max describe
    run-to-run variation on this machine rather than one number taken on trust.
    """
    payload = cbor.encode({"p": os.name, "m": "gpt-4o-mini", "ph": blake2b(b"x" * 1024),
                           "st": 1, "te": 2, "k": blake2b(b"y")})
    arms: dict[str, list[dict]] = {"mmap": [], "write": []}
    per_rate: list[dict] = []
    for rate in RATES:
        tmp_mmap, uuid = _mk_tape(column)
        w = MmapAppendTape(tmp_mmap, uuid, 256 << 20)
        tmp_write, uuid2 = _mk_tape(column)
        t = WriteAppendTape(tmp_write, uuid2, 256 << 20)
        blocks: dict[str, list[dict]] = {"mmap": [], "write": []}
        ratios = []
        sw0 = t.syscalls
        for i in range(pairs):
            order = ("mmap", "write") if i % 2 == 0 else ("write", "mmap")
            got = {}
            for arm in order:
                if arm == "mmap":
                    got["mmap"] = _paced_block(
                        lambda writer=w: writer.emit(C.KIND_MODEL_REQUEST, payload), rate, seconds)
                else:
                    got["write"] = _paced_block(
                        lambda tape=t: tape.emit(C.KIND_MODEL_REQUEST, payload), rate, seconds)
            for arm in ("mmap", "write"):
                blocks[arm].append(got[arm])
            a_lat = got["mmap"]["lat"]
            b_lat = got["write"]["lat"]
            a50 = statistics.median(a_lat)
            b50 = statistics.median(b_lat)
            a99 = statistics.quantiles(a_lat, n=100)[98]
            b99 = statistics.quantiles(b_lat, n=100)[98]
            ratios.append({"p50_ratio": round(b50 / a50, 3), "p99_ratio": round(b99 / a99, 3),
                           "mmap_p50_us": round(a50 / 1000, 3), "write_p50_us": round(b50 / 1000, 3),
                           "order": "".join(x[0].upper() for x in order)})
        w.flush()
        w.close()
        _fs.release(tmp_mmap)
        t.close()
        _fs.release(tmp_write)
        p50s = sorted(r["p50_ratio"] for r in ratios)
        p99s = sorted(r["p99_ratio"] for r in ratios)
        mmap_all = [x for b in blocks["mmap"] for x in b["lat"]]
        write_all = [x for b in blocks["write"] for x in b["lat"]]
        row = {"offered_ev_s": rate, "pairs": pairs,
               "mmap_p50_us": round(statistics.median(mmap_all) / 1000, 3),
               "mmap_p99_us": round(statistics.quantiles(mmap_all, n=100)[98] / 1000, 3),
               "write_p50_us": round(statistics.median(write_all) / 1000, 3),
               "write_p99_us": round(statistics.quantiles(write_all, n=100)[98] / 1000, 3),
               "p50_ratio_median": statistics.median(p50s), "p50_ratio_min": p50s[0],
               "p50_ratio_max": p50s[-1], "p99_ratio_median": statistics.median(p99s),
               "per_pair": ratios,
               "write_pwrite_calls": t.syscalls - sw0,
               "write_events": len(write_all),
               "pwrite_per_event": round((t.syscalls - sw0) / max(len(write_all), 1), 4)}
        per_rate.append(row)
        arms["mmap"].append({"offered_ev_s": rate, "events": len(mmap_all),
                             "p50_us": row["mmap_p50_us"], "p99_us": row["mmap_p99_us"]})
        arms["write"].append({"offered_ev_s": rate, "events": len(write_all),
                              "p50_us": row["write_p50_us"], "p99_us": row["write_p99_us"]})
    decision_rates = (10_000, 40_000)
    decision = [r for r in per_rate if r["offered_ev_s"] in decision_rates]
    adopt = all(r["p50_ratio_median"] <= 1.20 and r["write_p99_us"] <= 250.0 for r in decision)
    return {"filesystem": column["name"], "arms": arms, "per_rate": per_rate,
            "decision_rates": list(decision_rates),
            "rule": ("adopt write()-append iff the write arm's median p50 ratio <= 1.20 at BOTH decision rates "
                     "(10 k and 40 k ev/s) and its p99 <= 250 us there; 1 k ev/s is reported only, because the "
                     "5 ms window makes ~5-event batches and measures syscall amortisation, not the design"),
            "verdict": {"decision": "adopt write()-append" if adopt else "keep mmap",
                        "adopt_write": adopt},
            "interleaving": (f"{pairs} pairs per rate, order alternating (A,B then B,A), block = {seconds} s")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=1.0, help="length of one block")
    ap.add_argument("--pairs", type=int, default=10, help="interleaved A/B pairs per rate")
    ap.add_argument("--pairs-label", type=int, default=3, help="pairs for the non-gate (tmpfs) column")
    ap.add_argument("--profile", action="store_true", help="also run the C17 step-1 per-event profile")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    columns = _fs.columns()
    results: dict[str, dict] = {}
    for c in columns:
        pairs = args.pairs if c["gate"] else args.pairs_label
        results[c["name"]] = measure(c, args.seconds, pairs)
    out = {"date": time.strftime("%Y-%m-%d"), "probe": "P1-19 mmap vs write()-append (ADR-001 / C17 verdict)",
           "protocol": ("identical frame encoder, 16 KiB group commit, CHAIN frame and two-slot head publish; "
                        "only the store differs. The write() arm encodes into one preallocated bytearray with "
                        "pack_into, writes the batch with ONE pwrite and the CHAIN frame with a second, and "
                        "publishes the head through the same HeadMap"),
           "gate_column": _fs.GATE_FS, "hardware": hardware(),
           "columns": {c["name"]: {"path": _fs.label(c["dir"]), "gate": c["gate"], "note": c["note"]}
                       for c in columns},
           "results": results}
    if args.profile:
        gate = next(c for c in columns if c["gate"])
        out["profile"] = profile_arms(gate)
    gate_res = results[_fs.GATE_FS]
    out["verdict"] = {**gate_res["verdict"], "rule": gate_res["rule"],
                      "decision_rates": gate_res["decision_rates"],
                      "per_rate": [{k: r[k] for k in ("offered_ev_s", "p50_ratio_median", "p50_ratio_min",
                                                      "p50_ratio_max", "write_p99_us", "mmap_p99_us")}
                                   for r in gate_res["per_rate"]]}
    path = args.out or str(ROOT / "bench" / "results" / f"{out['date']}-writer-ab.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")
    if "profile" in out:
        pr = out["profile"]
        print(f"  profile  mmap {pr['mmap']['cpu_ns_per_event']} ns/ev, write {pr['write']['cpu_ns_per_event']} "
              f"ns/ev, difference {pr['difference_ns_per_event']} ns/ev, "
              f"write pwrite calls {pr['write']['pwrite_calls']}")
    for fs_name, block in out["results"].items():
        for r in block["per_rate"]:
            print(f"  [{fs_name:6s}] {r['offered_ev_s']:6d} ev/s mmap p50={r['mmap_p50_us']:8.3f} "
                  f"p99={r['mmap_p99_us']:8.3f} | write p50={r['write_p50_us']:8.3f} p99={r['write_p99_us']:8.3f} "
                  f"| p50 ratio median={r['p50_ratio_median']:.3f} [{r['p50_ratio_min']:.3f}, "
                  f"{r['p50_ratio_max']:.3f}] pwrite/event={r['pwrite_per_event']}")
    print(f"  verdict: {out['verdict']['decision']} (decision rates {out['verdict']['decision_rates']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
