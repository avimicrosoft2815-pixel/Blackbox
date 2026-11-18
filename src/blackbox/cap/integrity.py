"""`INTEGRITY.sha256` — one line per entry, one root over the lines (P4-03, §13.1, §13.4).

The capsule already carried this file; what P4-03 adds is that the check is a **function with a report** rather than a
loop inside the reader, and that the report **names the entry** whose bytes changed. A verifier that says "the capsule
was modified" has told a reader nothing they can act on; "`events/seg-000001.raw`: sha256 3f2a… does not match the
recorded 91be…" says which byte range to look at, and whether the change is in the raw evidence or in something derived
from it.

Four things can be wrong, and each is reported separately because they mean different things:

| finding | meaning |
|---|---|
| `mismatched` | the entry is there and its bytes are not the ones that were sealed — corruption, or a deliberate edit |
| `missing` | a line names an entry the capsule does not contain — a truncated archive, or an entry removed |
| `unlisted` | an entry is present that no line covers — an addition, which a case-folded duplicate check alone would miss |
| `malformed` | a line that is not `<64 hex>  <name>` — a damaged index, never silently skipped |

`root.ok` is separate: it is `sha256` over the *lines*, so it catches an edit to the index itself even when every entry
still hashes to what its (edited) line says. Both are checked, because either alone is forgeable by editing the other.

The ordering is fixed — lines sorted by name, `LF`-terminated, no trailing blank line when there are no entries — because
the same inputs must produce the same capsule bytes (I6). `sha256` is used as a digest, never as a signature: this file
proves the capsule is self-consistent, not who wrote it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from blackbox.cap import format as cap

NAME = cap.INTEGRITY_NAME

#: `<64 lower-case hex><two spaces><name>` — the format `sha256sum` writes, so an outside tool can read it too.
LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def lines(entries: dict[str, bytes]) -> bytes:
    """The canonical `INTEGRITY.sha256` body: one sorted line per entry, excluding the index and the manifest."""
    rows = [f"{hashlib.sha256(blob).hexdigest()}  {name}"
            for name, blob in sorted(entries.items())
            if name not in (cap.INTEGRITY_NAME, cap.MANIFEST_NAME)]
    return ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")


def root(lines: bytes) -> str:
    """The single value the manifest carries: `sha256` over the sorted lines, blank entries and all."""
    return hashlib.sha256(lines).hexdigest()


def parse(lines: bytes) -> tuple[dict[str, str], list[str]]:
    """`(name -> declared digest, malformed lines)`. A blank line is ignored; anything else must match `LINE_RE`."""
    digests: dict[str, str] = {}
    malformed: list[str] = []
    for raw in lines.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        match = LINE_RE.match(raw)
        if match is None:
            malformed.append(raw)
            continue
        digests[match.group(2)] = match.group(1)
    return digests, malformed


def verify(entries: dict[str, bytes], declared: bytes, declared_root: str | None = None) -> dict[str, Any]:
    """Compare a capsule's entries with its own `INTEGRITY.sha256`, naming every entry that disagrees.

    `entries` must include the index itself (it is simply not covered by it). Returns a report with the four findings
    above, the two roots, and a flat `problems` list a caller can print without interpreting the structure — the CLI
    prints those, and each one starts with the entry name so a reader can grep for it.
    """
    digests, malformed = parse(declared)
    mismatched: list[dict[str, str]] = []
    missing: list[str] = []
    matched = 0
    for name, digest in sorted(digests.items()):
        if name not in entries:
            missing.append(name)
            continue
        actual = hashlib.sha256(entries[name]).hexdigest()
        if actual != digest:
            mismatched.append({"name": name, "declared": digest, "actual": actual})
        else:
            matched += 1
    unlisted = sorted(name for name in entries if name not in digests
                      and name not in (cap.INTEGRITY_NAME, cap.MANIFEST_NAME))
    recomputed_root = root(declared)
    root_ok = declared_root is None or declared_root == recomputed_root
    problems: list[str] = []
    for item in mismatched:
        problems.append(f"{item['name']}: sha256 {item['actual'][:16]}… does not match the recorded "
                        f"{item['declared'][:16]}…")
    for name in missing:
        problems.append(f"{name}: listed in {NAME} but absent from the capsule")
    for name in unlisted:
        problems.append(f"{name}: present in the capsule but not listed in {NAME}")
    for raw in malformed:
        problems.append(f"{NAME}: malformed line {raw[:80]!r} (expected '<64 hex>  <name>')")
    if not root_ok:
        problems.append(f"integrity root {declared_root} does not match the index ({recomputed_root})")
    ok = not (mismatched or missing or unlisted or malformed) and root_ok
    return {"ok": ok, "entries": len(digests), "matched": matched, "mismatched": mismatched, "missing": missing,
            "unlisted": unlisted, "malformed": malformed,
            "root": {"declared": declared_root, "recomputed": recomputed_root, "ok": root_ok},
            "problems": problems}


__all__ = ["LINE_RE", "NAME", "lines", "parse", "root", "verify"]
