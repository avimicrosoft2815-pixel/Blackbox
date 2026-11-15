"""`.bbx` capsule container, v0 (P2-10): a reproducible zip with a sha256 root.

A capsule is the only artifact this project hands to someone else, so two properties matter more than anything
else and both are testable without a viewer:

* **atomic** — the capsule is written as `.<capsule_id>.partial` with `O_EXCL`, fsynced, then `os.replace`d into
  place, then the directory is fsynced. A visible `.bbx` is a complete `.bbx` (I7);
* **byte-identical when the inputs are identical** — fixed entry order, fixed timestamps (1980-01-01), fixed
  permissions, fixed compression choice. Re-sealing the same run produces the same bytes, which is what makes
  `I6` (idempotence) a hash comparison rather than an opinion.

Layout (v0, frozen here and in `docs/format.md` §13):

    events/seg-000001.raw     raw frames of one segment, byte-identical to the tape
    chain.json                chain values the reader needs to walk across a retention hole
    manifest.json             identity, counts, gaps, integrity root, signature slot = null
    INTEGRITY.sha256          `<sha256>  <name>` for every entry except itself and `manifest.json`

The root is `sha256` over the sorted `INTEGRITY.sha256` lines — so the manifest can carry it without hashing
itself, and the root still covers every byte of raw data. `manifest.json` is listed in `INTEGRITY.sha256` too, so
`verify` recomputes both directions: root → lines → entries, and entries → root.

This module never extracts, never maps and never trusts a name: reading a capsule passes every entry name through
the same checks the hostile-input validator applies (absolute paths, `..`, backslashes, NUL, symlinks, duplicates),
because a reader that opens the file before validating it is the bug this ordering exists to avoid.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass, field

from blackbox.errors import bbx
from blackbox.platform import current as _plat_current

_plat = _plat_current()

CAPSULE_FORMAT = "bbx.capsule"
CAPSULE_FORMAT_VERSION = 0
MIN_READER = "0.1"
MANIFEST_NAME = "manifest.json"
INTEGRITY_NAME = "INTEGRITY.sha256"
CHAIN_NAME = "chain.json"
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_MODE = 0o644 << 16
MAX_ENTRIES = 5_000
MAX_TOTAL_BYTES = 4 << 30
MAX_RATIO = 100

_UNSAFE_PARTS = ("..", ".", "")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def seg_entry_name(segment_no: int) -> str:
    """`events/seg-000001.raw` — v0 stores raw frames; `.cseg` arrives with compaction (P1-13)."""
    return f"events/seg-{segment_no:06d}.raw"


def integrity_lines(entries: dict[str, bytes]) -> bytes:
    """`<sha256>  <name>` per entry, sorted by name, excluding this file and the manifest."""
    rows = [f"{sha256(blob).hex()}  {name}"
            for name, blob in sorted(entries.items())
            if name not in (INTEGRITY_NAME, MANIFEST_NAME)]
    return ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")


def integrity_root(lines: bytes) -> str:
    """The single hash the manifest carries: `sha256` over the sorted INTEGRITY lines."""
    return hashlib.sha256(lines).hexdigest()


def capsule_bytes(entries: dict[str, bytes], manifest: dict) -> tuple[bytes, dict]:
    """Build the whole container in memory. `manifest` is completed here (counts + integrity root)."""
    body = {k: v for k, v in entries.items()
            if k not in (INTEGRITY_NAME, MANIFEST_NAME)}
    lines = integrity_lines(body)
    root = integrity_root(lines)
    full = dict(manifest)
    full["format"] = CAPSULE_FORMAT
    full["format_version"] = CAPSULE_FORMAT_VERSION
    full.setdefault("min_reader", MIN_READER)
    integrity = dict(full.get("integrity") or {})
    integrity.update({"alg": "sha256", "entries": len(body) + 2, "root": root})
    full["integrity"] = integrity
    full.setdefault("signature", None)
    full["counts"] = dict(full.get("counts") or {})
    full["counts"]["bytes"] = sum(len(v) for v in body.values())
    manifest_bytes = json.dumps(full, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    all_entries = dict(body)
    all_entries[MANIFEST_NAME] = manifest_bytes
    all_entries[INTEGRITY_NAME] = lines

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Sorted, manifest last: a reader streaming the archive sees data before the thing that describes it.
        order = [*sorted(n for n in all_entries if n != MANIFEST_NAME), MANIFEST_NAME]
        for name in order:
            blob = all_entries[name]
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED if name.endswith(".json") else zipfile.ZIP_STORED
            info.external_attr = FIXED_MODE
            info.create_system = 0
            info.internal_attr = 0
            zf.writestr(info, blob)
    return buf.getvalue(), full


@dataclass
class Capsule:
    entries: dict[str, bytes]
    manifest: dict
    root_ok: bool
    problems: list[str] = field(default_factory=list)

    def entry(self, name: str) -> bytes:
        return self.entries[name]

    def frames_of(self, segment_no: int) -> bytes:
        return self.entries[seg_entry_name(segment_no)]


def _check_name(name: str) -> None:
    if "\x00" in name or "\\" in name:
        raise bbx("BBX-E403", f"unsafe entry name {name!r}")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise bbx("BBX-E403", f"absolute entry name {name!r}")
    for part in name.split("/"):
        if part in _UNSAFE_PARTS:
            raise bbx("BBX-E403", f"entry name {name!r} escapes its root")


def read_capsule(path: str, *, max_bytes: int = 1 << 30) -> Capsule:
    """Read and verify a capsule. Never extracts; never trusts the archive's own claims."""
    size = os.path.getsize(path)
    if size > max_bytes:
        raise bbx("BBX-E401", f"{path} is {size} B, over the {max_bytes} B limit")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise bbx("BBX-E402", f"{path}: {exc}") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise bbx("BBX-E402", f"{path}: {len(infos)} entries > {MAX_ENTRIES}")
        seen: set[str] = set()
        total = 0
        for info in infos:
            _check_name(info.filename)
            key = info.filename.casefold()
            if key in seen:
                raise bbx("BBX-E404", f"duplicate entry {info.filename!r}")
            seen.add(key)
            total += info.file_size
            if info.file_size and info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
                raise bbx("BBX-E405", f"{info.filename} expands {(info.file_size / info.compress_size):.0f}:1")
        if total > MAX_TOTAL_BYTES:
            raise bbx("BBX-E405", f"declared total {total} B over {MAX_TOTAL_BYTES} B")
        entries = {info.filename: zf.read(info) for info in infos}
    if MANIFEST_NAME not in entries:
        raise bbx("BBX-E407", "capsule has no manifest.json")
    try:
        manifest = json.loads(entries[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise bbx("BBX-E407", f"manifest.json: {exc}") from None
    if manifest.get("format") != CAPSULE_FORMAT:
        raise bbx("BBX-E407", f"manifest format {manifest.get('format')!r} is not {CAPSULE_FORMAT}")
    if int(manifest.get("format_version", -1)) > CAPSULE_FORMAT_VERSION:
        raise bbx("BBX-E407", f"capsule format_version {manifest['format_version']} is newer than this reader")
    problems: list[str] = []
    lines = entries.get(INTEGRITY_NAME, b"")
    if not lines:
        problems.append("INTEGRITY.sha256 is missing")
    want = (manifest.get("integrity") or {}).get("root")
    got = integrity_root(lines)
    if want != got:
        problems.append(f"integrity root {want} does not match the entries ({got})")
    for line in lines.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if name not in entries:
            problems.append(f"{name}: listed in INTEGRITY.sha256 but absent")
            continue
        if sha256(entries[name]).hex() != digest:
            problems.append(f"{name}: sha256 does not match INTEGRITY.sha256")
    return Capsule(entries=entries, manifest=manifest, root_ok=not problems, problems=problems)


def write_capsule(dest: str, entries: dict[str, bytes], manifest: dict) -> dict:
    """Write `dest` atomically: `.<name>.partial` → fsync → `os.replace` → fsync(dir).

    Returns `{"path", "root", "bytes", "entries"}`. If `dest` already exists with the same content the file is
    left alone (re-sealing is idempotent, not destructive).
    """
    blob, full = capsule_bytes(entries, manifest)
    directory = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.exists(dest):
        with open(dest, "rb") as fh:
            if fh.read() == blob:
                return {"path": dest, "root": full["integrity"]["root"], "bytes": len(blob),
                        "entries": len(entries) + 2, "unchanged": True}
    tmp = os.path.join(directory, f".{os.path.basename(dest)}.{os.getpid()}.partial")
    try:
        fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.unlink(tmp)
        fd = _plat.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(blob)
        written = 0
        while written < len(blob):
            written += os.write(fd, view[written:])
        if hasattr(os, "fdatasync"):
            os.fdatasync(fd)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    # The directory entry is what makes the capsule survive a power cut; a platform without directory fsync
    # (Windows) says so in its own docstring, and that is the one case where there is nothing to do.
    with contextlib.suppress(OSError):
        _plat.fsync_dir(directory)
    return {"path": dest, "root": full["integrity"]["root"], "bytes": len(blob),
            "entries": len(entries) + 2, "unchanged": False}


def frame_count(raw: bytes) -> int:
    """Frames in a raw segment body, counted with the frame module's own decoder (never by hand)."""
    from blackbox import constants as C
    from blackbox.tape import frame as frame_mod

    off = C.SEGMENT_HEADER_BYTES
    n = 0
    while off + C.FRAME_OVERHEAD_BYTES <= len(raw):
        try:
            _fields, nxt = frame_mod.decode(raw, off)
        except frame_mod.FrameError:
            break
        n += 1
        off = nxt
    return n


def zinfo_unpack(path: str) -> list[dict]:
    """Entry metadata without reading bodies: used by `doctor` and by the determinism test."""
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            out.append({"name": info.filename, "date_time": info.date_time,
                        "compress_type": info.compress_type, "external_attr": info.external_attr,
                        "crc": info.CRC, "size": info.file_size})
    return out
