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
import struct
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

from blackbox.errors import bbx
from blackbox.platform import current as _plat_current
from blackbox.platform import replace as _replace

_plat = _plat_current()

CAPSULE_FORMAT = "bbx.capsule"
CAPSULE_FORMAT_VERSION = 0
MIN_READER = "0.1"
MANIFEST_NAME = "manifest.json"
INTEGRITY_NAME = "INTEGRITY.sha256"
CHAIN_NAME = "chain.json"
# §11.10: the cross-vantage links, with their class, evidence and confidence. Written at seal time by the resolver,
# never appended to a tape (see rec/correlate.py).
LINKS_NAME = "links.json"
# D-053/§13.1: the graph the run had — nodes and the edges the links justify. A *derived* part: it is recomputed from
# the raw segments (plus `links.json`) by `cap.derived`, declared in `manifest.derived_parts[]`, and drift is BBX-E412.
GRAPH_NAME = "graph.json"
EVENTS_NAME = "events/events.jsonl"      # P4-13: the timeline as one JSON object per frame
OTLP_NAME = "otlp/spans.json"            # P4-14: the same frames as OTLP/JSON ResourceSpans
OTLP_PB_NAME = "otlp/spans.pb"         # P4-20: and as OTLP protobuf, the encoding a receiver takes
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_MODE = 0o644 << 16
MAX_ENTRIES = 5_000
MAX_TOTAL_BYTES = 4 << 30
MAX_RATIO = 100

_UNSAFE_PARTS = ("..", ".", "")

#: §13.4 check 6: the compression methods a capsule may use. `zipfile` will happily open bzip2 and lzma entries; a
#: reader that accepts them accepts a decoder this format never promised, so both are refused with the same code.
_METHODS_OK = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

#: §13.4 check 3: an archive member may carry a mode. Nothing in this format writes one, so a symlink, device, fifo or
#: socket entry is a capsule trying to be a filesystem — refused before any byte is read.
_UNIX_MODE_MASK = 0o170000
_HOSTILE_MODES = {0o120000: "symlink", 0o020000: "character device", 0o060000: "block device",
                  0o010000: "fifo", 0o140000: "socket"}

#: The chunk size of the bounded extraction read (§13.4 check 9). One MiB: large enough that the loop is not the cost,
#: small enough that a lying header cannot make the reader allocate the lie.
_READ_CHUNK = 1 << 20


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def seg_entry_name(segment_no: int) -> str:
    """`events/seg-000001.raw` — v0 stores raw frames; `.cseg` arrives with compaction (P1-13)."""
    return f"events/seg-{segment_no:06d}.raw"


def integrity_lines(entries: dict[str, bytes]) -> bytes:
    """`<sha256>  <name>` per entry, sorted by name, excluding this file and the manifest (P4-03 moved the body to
    `cap/integrity.py`; this name stays because callers and `docs/format.md` already use it)."""
    from blackbox.cap import integrity

    return integrity.lines(entries)


def integrity_root(lines: bytes) -> str:
    """The single hash the manifest carries: `sha256` over the sorted INTEGRITY lines."""
    from blackbox.cap import integrity

    return integrity.root(lines)


def capsule_bytes(entries: dict[str, bytes], manifest: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
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
    # P4-02: a writer must not produce a manifest it would refuse to read. The check is after completion (format,
    # integrity and signature are filled in above) and before a single byte is written.
    from blackbox.cap import manifest as manifest_mod

    manifest_mod.validate(full, where="capsule write")
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
    manifest: dict[str, Any]
    root_ok: bool
    problems: list[str] = field(default_factory=list)
    #: Manifest keys this build does not know. Tolerated by design (a newer writer's fields), carried so a caller can
    #: say "this capsule was written by something ahead of me" instead of guessing from an empty error list.
    unknown_manifest_keys: list[str] = field(default_factory=list)

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


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, remaining: int) -> bytes:
    """Read one entry in bounded chunks and refuse a header that lied about its size (§13.4 check 9).

    `zipfile` bounds its own reads by the declared `file_size`, so this counter is a second net rather than the first —
    which is exactly why it is cheap: it costs one comparison per MiB and it is the only place a *future* reader that
    does not trust `zipfile` can see the lie. `remaining` is what is left of the 4 GiB declared total, enforced on the
    bytes that actually arrive rather than on the total the archive claims.
    """
    out = bytearray()
    try:
        with zf.open(info) as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                out += chunk
                if len(out) > info.file_size:
                    raise bbx("BBX-E409", f"{info.filename} produced more than its declared {info.file_size} B")
                if len(out) > remaining:
                    raise bbx("BBX-E405", f"{info.filename} pushes the capsule past {MAX_TOTAL_BYTES} B")
    except zipfile.BadZipFile as exc:
        raise bbx("BBX-E402", f"{info.filename}: {exc}") from None
    return bytes(out)



# ── refusing an oversized archive without parsing it (defect 117) ─────────────────────────────────────────────────────

_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_MIN = 22
_ZIP_COMMENT_MAX = 65_535


def declared_entry_count(path: str) -> int | None:
    """The entry count the archive's **end-of-central-directory record** declares, or `None` if it cannot be read.

    `zipfile.ZipFile(path)` parses every central-directory record before anything in this module can count them, and that
    parse is proportional to the number of entries: 20 000 entries cost 159 ms on this machine while reading the end record
    costs 0.07 ms (defect 117). The end record is fixed-size and sits in the last 22 bytes plus at most 65 535 bytes of
    comment, so the count is available before the directory is touched — which is what lets an oversized archive be refused
    in constant work, and what makes the 50 ms budget reachable on a machine that is slow at parsing.

    `None` means "ask the parser": the file is too short, has no end record in its last 65 557 bytes, or declares 0xFFFF,
    which is the marker zip64 uses when the real count lives in a second record. This function never decides that an archive
    is *acceptable* — only `_check_infos` does that, after the parse, so a lying end record still gets refused.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            start = max(0, size - (_EOCD_MIN + _ZIP_COMMENT_MAX))
            handle.seek(start)
            tail = handle.read()
    except OSError:
        return None
    index = tail.rfind(_EOCD_SIGNATURE)
    if index < 0 or len(tail) - index < _EOCD_MIN:
        return None
    try:
        _sig, disk, cd_disk, on_disk, total, _cd_size, _cd_off, comment_len = struct.unpack_from(
            "<4s4H2IH", tail, index
        )
    except struct.error:                                       # pragma: no cover - len() already guarantees 22
        return None
    # A valid EOCD sits at the exact end of the file: its declared comment length must match the trailing bytes.
    # Otherwise `PK\x05\x06` inside an entry payload or comment could trigger a false-positive refusal.
    if comment_len != len(tail) - index - _EOCD_MIN:
        return None
    if disk != 0 or cd_disk != 0 or on_disk != total:
        return None
    if total == 0xFFFF:                                        # zip64: the real count is elsewhere
        return None
    return total


def _check_infos(path: str, infos: list[zipfile.ZipInfo]) -> None:
    """The checks that need only the archive's own headers — §13.4 checks 2, 3, 4, 5 and 6, before any body is read.

    Split out so `read_manifest` (the library's fast path) gets the same refusals as `read_capsule`: a directory listing
    that skipped them would be a second, weaker reader of untrusted files.
    """
    if len(infos) > MAX_ENTRIES:
        raise bbx("BBX-E402", f"{path}: {len(infos)} entries > {MAX_ENTRIES}")
    seen: set[str] = set()
    total = 0
    for info in infos:
        _check_name(info.filename)
        mode = (info.external_attr >> 16) & _UNIX_MODE_MASK
        if mode in _HOSTILE_MODES:
            raise bbx("BBX-E403", f"{info.filename} is a {_HOSTILE_MODES[mode]} entry")
        if info.flag_bits & 0x1:
            raise bbx("BBX-E406", f"{info.filename} is encrypted; a capsule is not a vault")
        if info.compress_type not in _METHODS_OK:
            raise bbx("BBX-E406", f"{info.filename} uses compression method {info.compress_type}, "
                                  f"which this format does not define")
        key = info.filename.casefold()
        if key in seen:
            raise bbx("BBX-E404", f"duplicate entry {info.filename!r}")
        seen.add(key)
        total += info.file_size
        if info.file_size and info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
            raise bbx("BBX-E405", f"{info.filename} expands {(info.file_size / info.compress_size):.0f}:1")
    if total > MAX_TOTAL_BYTES:
        raise bbx("BBX-E405", f"declared total {total} B over {MAX_TOTAL_BYTES} B")


def read_manifest(path: str, *, max_bytes: int = 1 << 30) -> dict[str, Any]:
    """Read **only** `manifest.json`, with every header check `read_capsule` makes and no entry bodies.

    This is what a library view needs: a hundred capsules on a screen is a hundred manifest reads, not a hundred
    capsules decompressed and hashed. What it does *not* do is verify the integrity index — that is `read_capsule`'s job,
    and the reason a library row says `unverified` rather than `ok` until someone opens the run.
    """
    size = os.path.getsize(path)
    if size > max_bytes:
        raise bbx("BBX-E401", f"{path} is {size} B, over the {max_bytes} B limit")
    declared = declared_entry_count(path)
    if declared is not None and declared > MAX_ENTRIES:
        raise bbx("BBX-E402", f"{path}: {declared} entries > {MAX_ENTRIES}")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise bbx("BBX-E402", f"{path}: {exc}") from None
    with zf:
        infos = zf.infolist()
        _check_infos(path, infos)
        wanted = [info for info in infos if info.filename == MANIFEST_NAME]
        if not wanted:
            raise bbx("BBX-E407", f"{path}: capsule has no manifest.json")
        info = wanted[0]
        if info.file_size > MAX_RATIO * 1024 * 1024:
            raise bbx("BBX-E405", f"manifest.json declares {info.file_size} B")
        raw = _read_entry(zf, info, remaining=MAX_TOTAL_BYTES)
    manifest = _parse_manifest(raw, where=path)
    from blackbox.cap import manifest as manifest_mod_read

    manifest_mod_read.validate(manifest, where=path)
    _check_declared_parts(path, manifest, {entry.filename for entry in infos})
    return manifest


def _parse_manifest(raw: bytes, *, where: str) -> dict[str, Any]:
    """Decode and validate a manifest body. Shared by both readers so a capsule cannot be acceptable to one of them."""
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise bbx("BBX-E407", f"manifest.json: {type(exc).__name__}: {exc}") from None
    if not isinstance(manifest, dict):
        raise bbx("BBX-E407", f"manifest.json is {type(manifest).__name__}, not an object")
    if manifest.get("format") != CAPSULE_FORMAT:
        raise bbx("BBX-E407", f"manifest format {manifest.get('format')!r} is not {CAPSULE_FORMAT}")
    if int(manifest.get("format_version", -1)) > CAPSULE_FORMAT_VERSION:
        raise bbx("BBX-E407", f"capsule format_version {manifest['format_version']} is newer than this reader")
    return manifest


def _check_declared_parts(path: str, manifest: dict[str, Any], names: set[str]) -> None:
    """§13.4 check 8, the half the integrity file cannot do: a manifest that *lists* a part the archive does not carry
    describes a capsule that is not this one.

    The hashes would verify over the entries that exist; the claim about what exists is what is checked here. It costs a
    set lookup, so the library's fast path makes it too — a capsule the library offers to open must be one that opens.
    """
    for part in manifest.get("derived_parts") or []:
        name = part.get("name")
        if isinstance(name, str) and name not in names:
            raise bbx("BBX-E408", f"manifest lists {name!r}, which the archive does not contain")


def _clock() -> tuple[int, int, int]:
    """(monotonic ns, this process's CPU ns, this thread's CPU ns) — three different questions (defect 117).

    Wall time says how long the caller waited; process CPU says how much CPU *anything in this process* burned; thread CPU
    says how much of it was this call. A refusal that shows 112 ms of wall, 109 ms of process CPU and 12 ms of thread CPU is
    a call that waited while something else in the same process ran, and the older two-clock message could not say that.
    `time.thread_time` is available on POSIX and Windows; where it is not, the thread figure is reported as -1 rather than
    guessed.
    """
    try:
        thread = time.thread_time_ns()
    except (AttributeError, OSError):     # pragma: no cover - platform without a thread clock
        thread = -1
    return time.perf_counter_ns(), time.process_time_ns(), thread


def _record(timing: dict[str, Any] | None, stage: str, started: tuple[int, int, int]) -> None:
    """Add one stage's three durations to `timing`, if the caller asked for them. Costs nothing when it did not."""
    if timing is None:
        return
    wall, cpu, thread = _clock()
    wall_ms = (wall - started[0]) / 1e6
    cpu_ms = (cpu - started[1]) / 1e6
    thread_ms = -1.0 if thread < 0 or started[2] < 0 else (thread - started[2]) / 1e6
    timing[stage] = {"wall_ms": round(wall_ms, 3), "process_ms": round(cpu_ms, 3), "thread_ms": round(thread_ms, 3)}


def read_capsule(path: str, *, max_bytes: int = 1 << 30, timing: dict[str, Any] | None = None) -> Capsule:
    """Read and verify a capsule. Never extracts; never trusts the archive's own claims.

    `timing` is an optional dict — passed by tests and by `tools/` when a refusal misses its budget — that receives one
    `{wall_ms, process_ms, thread_ms}` entry per stage (`size`, `open`, `infolist`, `entries`, `manifest`, `integrity`). It is
    measurement only: no stage behaves differently when it is passed, and nothing here is imported unless it is used.
    """
    stage = _clock()
    size = os.path.getsize(path)
    _record(timing, "size", stage)
    if size > max_bytes:
        raise bbx("BBX-E401", f"{path} is {size} B, over the {max_bytes} B limit")
    stage = _clock()
    declared = declared_entry_count(path)
    if declared is not None and declared > MAX_ENTRIES:
        raise bbx("BBX-E402", f"{path}: {declared} entries > {MAX_ENTRIES}")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        _record(timing, "open", stage)
        raise bbx("BBX-E402", f"{path}: {exc}") from None
    _record(timing, "open", stage)
    with zf:
        stage = _clock()
        infos = zf.infolist()
        _record(timing, "infolist", stage)
        stage = _clock()
        try:
            _check_infos(path, infos)
        finally:
            _record(timing, "check_infos", stage)
        stage = _clock()
        entries: dict[str, bytes] = {}
        for info in infos:
            entries[info.filename] = _read_entry(zf, info, remaining=MAX_TOTAL_BYTES - sum(len(v) for v in entries.values()))
        _record(timing, "entries", stage)
    if MANIFEST_NAME not in entries:
        raise bbx("BBX-E407", "capsule has no manifest.json")
    # Shared with `read_manifest` (defect 83's guards: non-object, deep nesting, schema, version).
    manifest = _parse_manifest(entries[MANIFEST_NAME], where=path)
    from blackbox.cap import manifest as manifest_mod_read
    unknown = manifest_mod_read.validate(manifest, where=path)
    _check_declared_parts(path, manifest, set(entries))
    from blackbox.cap import integrity

    declared_lines = entries.get(INTEGRITY_NAME, b"")
    wanted_root = (manifest.get("integrity") or {}).get("root")
    report = integrity.verify(entries, declared_lines, wanted_root)
    problems: list[str] = []
    if not declared_lines:
        problems.append("INTEGRITY.sha256 is missing")
    problems.extend(report["problems"])
    capsule = Capsule(entries=entries, manifest=manifest, root_ok=not problems, problems=problems)
    capsule.unknown_manifest_keys = unknown
    return capsule


def write_capsule(dest: str, entries: dict[str, bytes], manifest: dict[str, Any]) -> dict[str, Any]:
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
        _plat.fdatasync(fd)
    finally:
        os.close(fd)
    # A `verify`/`diagnose` that has the old capsule open stops the rename on Windows; the shim waits that out and
    # refuses with a code if it never ends (defect 105).
    _replace(tmp, dest)
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


def zinfo_unpack(path: str) -> list[dict[str, Any]]:
    """Entry metadata without reading bodies: used by `doctor` and by the determinism test."""
    out = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            out.append({"name": info.filename, "date_time": info.date_time,
                        "compress_type": info.compress_type, "external_attr": info.external_attr,
                        "crc": info.CRC, "size": info.file_size})
    return out
