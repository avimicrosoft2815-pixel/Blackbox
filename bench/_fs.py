"""C1 — which filesystem a number was measured on, and where the scratch dirs live.

A number is only gate evidence on a real disk: on tmpfs `fdatasync` is a no-op, so a durability claim measured
there is not a claim about any storage device. Every bench therefore runs the *same* protocol twice and labels
the columns; the gate reads `ext4`, the tmpfs column stays in the JSON as a second column.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_FS = "ext4"


def detect_fstype(path: str | os.PathLike[str]) -> str:
    """Longest-prefix match against /proc/mounts; 'unknown' where /proc is unavailable (macOS, Windows)."""
    target = Path(path).resolve()
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unknown"
    best, best_len = "unknown", -1
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if (str(target) == mount or str(target).startswith(mount.rstrip("/") + "/")) and len(mount) > best_len:
            best, best_len = parts[2], len(mount)
    return best


def columns() -> list[dict]:
    """The two columns a bench reports: the real disk (gate) and tmpfs (labelled, never gate evidence)."""
    out = []
    disk_dir = ROOT / ".bench" / "scratch"
    disk_dir.mkdir(parents=True, exist_ok=True)
    out.append({"name": detect_fstype(disk_dir), "dir": str(disk_dir), "gate": True,
                "note": "gate column: numbers here are measured on the repository's real filesystem"})
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK) and detect_fstype(shm) == "tmpfs":
        out.append({"name": "tmpfs", "dir": shm, "gate": False,
                    "note": "labelled second column only: fdatasync is a no-op on tmpfs, so no durability claim"})
    return out


def scratch(column: dict, prefix: str) -> str:
    os.makedirs(column["dir"], exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=column["dir"])


def release(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
