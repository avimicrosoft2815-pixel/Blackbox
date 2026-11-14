"""Writer/recovery leases (P2-01, docs/format.md §8).

A lease is a small JSON file created with `O_CREAT|O_EXCL`. It is stale iff the boot id differs, the pid is
gone, the pid is alive but its start token differs (PID reuse), or the file is older than 24 h with no frame
progress. A live lease is never stolen.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from blackbox import constants as C
from blackbox.errors import bbx
from blackbox.platform import posix


@dataclass(frozen=True)
class Lease:
    pid: int
    proc_start: int | None
    boot_id: str | None
    nonce: str
    created_ns: int
    path: str

    def to_json(self) -> bytes:
        return json.dumps({"v": 1, "pid": self.pid, "proc_start_ns": self.proc_start,
                           "boot_id": self.boot_id, "nonce": self.nonce,
                           "created_ns": self.created_ns}, sort_keys=True).encode()

    def is_stale(self, *, now_ns: int | None = None) -> tuple[bool, str]:
        now = now_ns or time.time_ns()
        current_boot = posix.boot_id()
        if self.boot_id and current_boot and self.boot_id != current_boot:
            return True, "rebooted"
        if not posix.pid_alive(self.pid):
            return True, "pid gone"
        if self.proc_start is not None:
            current = posix.proc_start_ticks(self.pid)
            if current is not None and current != self.proc_start:
                return True, "pid reused"
        if now - self.created_ns > C.LEASE_MAX_AGE_NS:
            return True, "aged out"
        return False, "live"


def acquire(path: str, *, pid: int | None = None) -> Lease:
    pid = pid or os.getpid()
    start = posix.proc_start_ticks(pid)
    lease = Lease(pid=pid, proc_start=start, boot_id=posix.boot_id(),
                  nonce=os.urandom(8).hex(), created_ns=time.time_ns(), path=path)
    payload = lease.to_json()
    if len(payload) > C.LEASE_MAX_BYTES:
        raise bbx("BBX-E210", "lease payload too large")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = read(path)
        if existing is not None:
            stale, why = existing.is_stale()
            if not stale:
                raise bbx("BBX-E210", f"lease held by pid {existing.pid} ({why})") from None
            os.replace(path, path + ".stale")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    return lease


def read(path: str) -> Lease | None:
    try:
        with open(path, "rb") as fh:
            d = json.loads(fh.read().decode())
    except (OSError, ValueError):
        return None
    return Lease(pid=int(d.get("pid", 0)), proc_start=d.get("proc_start_ns"),
                 boot_id=d.get("boot_id"), nonce=str(d.get("nonce", "")),
                 created_ns=int(d.get("created_ns", 0)), path=path)


def release(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def is_live(path: str) -> bool:
    lease = read(path)
    if lease is None:
        return False
    stale, _ = lease.is_stale()
    return not stale
