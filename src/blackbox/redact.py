"""Redaction, applied **before** bytes reach the tape (P3-11).

The rule this file enforces is the architecture's, not mine: content is redacted before persistence, not on the way
out. A secret that is written and then hidden is a secret that is on the disk, in the capsule, in someone's backup,
and in the diff of a support ticket. So this sits between the seam and the sink, and what the sink writes is already
clean.

Three things make it harder than `re.sub`, and each has a test:

1. **A secret can straddle two chunks.** A key split across a 512-byte read is invisible to a per-chunk regex, and
   the two halves go to disk as harmless-looking text. So the redactor releases nothing it cannot prove is finished:
   text is held only while it is *unfinished* — a credential's identifier with a body still growing (`PENDING`), a
   bare token run (`RUN_HOLD`), or a complete match that ends exactly at the end of what has arrived and so may
   continue. Prose, JSON and SSE framing carry no unfinished credential and are released on the chunk that brought
   them, which is why a `kill -9` still costs nothing but the last partial token instead of the last `WINDOW` bytes.
2. **The output must not grow without bound.** A redactor that buffered the stream would be the memory leak the
   seams exist to avoid: whatever the reason to hold, the hold is clamped to `WINDOW`, so it holds at most
   `WINDOW + one chunk` — and a credential longer than `WINDOW` can be split, which the docs say rather than deny.
3. **Nothing it keeps may be content.** It counts matches by kind and stores no bytes: `[REDACTED:openai]` in the
   tape is a fact about the stream, and the key itself is nowhere.

Modes, because "redact" is not one policy:

* `full` (the default) — `[REDACTED:openai]`.
* `hash` — `[REDACTED:openai:a1b2c3d4]`: a stable pseudonym, so two appearances of the same key can be correlated
  without the key being recoverable. A keyed digest would be stronger; a bare sha256 of a secret is brute-forceable
  for short secrets, which is why the placeholder says which one it is and the docs say what it means.
* `off` — no redaction, on purpose, for a run whose content is already synthetic. It is a decision a user makes
  explicitly, and it is recorded in the capsule as such.

If the redaction itself fails, the sink writes `FAILED` in place of the chunk and counts it (C4, fail closed): the
call keeps its place in the stream, its content does not appear, and a reader can see that something was lost.

**What this cannot do** — and the reason the docs do not say "safe": pattern matching finds the secrets someone wrote
a pattern for. A bespoke token in a bespoke format, a secret in a form this file has never seen, or a credential
inside a binary blob will pass through. The corpus test proves the 60 shapes below are caught; it does not prove the
next one is.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# The hard ceiling on what may be held back, whatever the reason. Larger than any credential in the corpus, so a
# secret cannot be split by the release point; a secret longer than this can still be split, and `SUPPORTED.md` says
# so instead of implying otherwise.
WINDOW = 4096
# How much of a bare run of token characters at the end of the buffer is held: enough to cover a credential whose
# first character has arrived and whose identifier is not yet recognisable (`s`, `sk`, `pers`), and no more, because a
# long run is a stream that is not finished rather than a secret that is unfinished.
RUN_HOLD = 16
RUN_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-+/=")

# What replaces a chunk when the redactor itself fails. The architecture's C4 says fail **closed**: the event is
# still recorded, the content is not, and the reader can see that something was lost and why.
FAILED = b"[REDACTION_FAILED]"

MODES = ("full", "hash", "off")
ENV_MODE = "BLACKBOX_REDACTION"
ENV_KINDS = "BLACKBOX_REDACTION_KINDS"

# The *beginnings* of each kind: what a credential looks like while it is still arriving. Each one may end in a
# partial body, which is the point — a hold that only recognises finished credentials is a hold that releases
# `sk-ant-api03-` on one read and leaks the rest on the next. They mirror `PATTERNS` one for one; a new pattern
# without a matching entry here is a split the corpus test will find.
PENDING: tuple[tuple[str, bytes], ...] = (
    # From the `BEGIN` line to now, unless a *complete* closing line has arrived: the hold stops only at
    # `-----END … PRIVATE KEY-----`, so a half-arrived `-----END` keeps the whole block held rather than releasing the
    # body and then failing to recognise what is left of it.
    ("private_key", rb"-----BEGIN(?:(?!--END [A-Z ]*PRIVATE KEY-----)[\s\S])*\Z"),
    ("anthropic", rb"sk-ant-[A-Za-z0-9_\-]*\Z"),
    ("openai", rb"sk-[A-Za-z0-9_\-]*\Z"),
    ("aws", rb"(?:AKIA|ASIA)[0-9A-Z]*\Z"),
    ("github", rb"gh[pousr]_[A-Za-z0-9]*\Z"),
    ("slack", rb"xox[baprs]-[A-Za-z0-9\-]*\Z"),
    ("jwt", rb"eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*){0,2}\Z"),
    ("bearer", rb"(?i)bearer\s*[A-Za-z0-9._\-]*\Z"),
    ("assignment", rb"(?i)(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\"?\s*"
                   rb"(?:[:=]\s*\"?[A-Za-z0-9._\-+/=]*)?\Z"),
    ("email", rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]*\Z"),
)

# Order matters: `sk-ant-` before `sk-`, and the private-key block before the generic assignment rule, or a shorter
# pattern would claim the text first and leave the rest of the longer secret in place.
PATTERNS: tuple[tuple[str, bytes], ...] = (
    ("private_key", rb"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic", rb"sk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openai", rb"sk-[A-Za-z0-9_\-]{16,}"),
    ("aws", rb"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    ("github", rb"gh[pousr]_[A-Za-z0-9]{16,}"),
    ("slack", rb"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    ("jwt", rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ("bearer", rb"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    ("assignment", rb"(?i)(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\"?\s*[:=]\s*\"?"
                   rb"[A-Za-z0-9._\-+/=]{12,}"),
    ("email", rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
)

_COMPILED = tuple((kind, re.compile(pattern)) for kind, pattern in PATTERNS)
_PENDING = tuple((kind, re.compile(pattern)) for kind, pattern in PENDING)

# A literal every match of a kind must contain, lowercased. Checked before the regex runs: an unanchored
# `[A-Za-z0-9._%+-]+@` re-scans the whole buffer from every position when there is no `@` in it at all, which turns a
# 4 KiB read into 16 million character steps and a redactor that costs more than the model call it is recording.
_NEEDLES: dict[str, tuple[bytes, ...]] = {
    "private_key": (b"-----begin",),
    "anthropic": (b"sk-ant-",),
    "openai": (b"sk-",),
    "aws": (b"akia", b"asia"),
    "github": (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_"),
    "slack": (b"xox",),
    "jwt": (b"eyj",),
    "bearer": (b"bearer",),
    "assignment": (b"token", b"secret", b"password", b"passwd", b"api_key", b"api-key", b"apikey",
                   b"access_key", b"access-key"),
    "email": (b"@",),
}


def _possible(kind: str, lower: bytes) -> bool:
    """Whether this buffer could contain a match of `kind` at all — a substring test, not a regex."""
    needles = _NEEDLES.get(kind)
    return needles is None or any(needle in lower for needle in needles)


def _gaps(length: int, claimed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The stretches of a buffer no pattern has claimed yet, so a later pattern searches around, never across."""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in claimed:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


class Redactor:
    """Feed it chunks; it returns the bytes that are safe to persist. `flush()` ends the stream."""

    @classmethod
    def from_config(cls) -> Redactor:
        """The policy a process gets without arguing: `full`, unless `BLACKBOX_REDACTION` says otherwise.

        Redaction is on by default because the alternative default — write everything, redact later — is the one
        that fails silently: the bytes are already on the disk, in the capsule and in whatever the user exports. A
        user who wants raw content for a synthetic run sets `BLACKBOX_REDACTION=off` and the capsule records that
        they did.
        """
        mode = (os.environ.get(ENV_MODE) or "full").strip().lower()
        if mode not in MODES:
            mode = "full"                                   # a typo must not quietly disable redaction
        kinds_env = os.environ.get(ENV_KINDS, "").strip()
        kinds = tuple(part.strip() for part in kinds_env.split(",") if part.strip()) or None
        return cls(mode, kinds=kinds)

    """Feed it chunks; it returns the bytes that are safe to persist. `flush()` ends the stream."""

    def __init__(self, mode: str = "full", *, window: int = WINDOW,
                 kinds: tuple[str, ...] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown redaction mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.window = window
        self.kinds = kinds or tuple(kind for kind, _ in PATTERNS)
        self.counts: dict[str, int] = {}
        self.bytes_in = 0
        self.bytes_out = 0
        self._buf = b""

    # ── the streaming interface ─────────────────────────────────────────────
    def feed(self, chunk: bytes) -> bytes:
        self.bytes_in += len(chunk)
        if self.mode == "off":
            self.bytes_out += len(chunk)
            return chunk
        self._buf += chunk
        matches = self._matches(self._buf)
        release = self._cut(self._buf, matches)
        out = self._replace(self._buf[:release], [m for m in matches if m.end() <= release])
        self._buf = self._buf[release:]
        self.bytes_out += len(out)
        return out

    def _cut(self, buf: bytes, matches: list[re.Match[bytes]]) -> int:
        """How much of `buf` can be written now. Everything, unless something in it is unfinished."""
        end = len(buf)
        cut = end
        lower = buf.lower()
        for kind, pattern in _PENDING:
            if kind not in self.kinds or not _possible(kind, lower):
                continue
            found = pattern.search(buf)
            if found is not None:
                cut = min(cut, found.start())           # an identifier whose body is still arriving
        for match in matches:
            if match.end() == end:
                cut = min(cut, match.start())           # a complete match at the edge may grow into a longer one
            elif match.start() < cut < match.end():
                cut = match.start()                     # never ship the prefix of a match we then cannot see whole
        run = 0
        while run < end and buf[end - 1 - run] in RUN_CHARS:
            run += 1
        if run:
            cut = min(cut, end - min(run, RUN_HOLD))    # a token that may still be growing
        return max(cut, end - self.window)               # the memory bound wins over every reason to hold

    def flush(self) -> bytes:
        if self.mode == "off":
            return b""
        buf = self._buf + b""
        self._buf = b""
        out = self._replace(buf, self._matches(buf))
        self.bytes_out += len(out)
        return out

    # ── internals ───────────────────────────────────────────────────────────
    def _matches(self, buf: bytes) -> list[re.Match[bytes]]:
        """Every distinct secret in `buf`, longest-first by pattern priority, none overlapping another.

        Each pattern searches only the gaps the earlier patterns left. That matters more than it looks: the private
        key's closing line `-----END RSA PRIVATE KEY-----person@host` is a legal start for the email pattern (a dash
        is a legal local-part character), so a pattern that searched the whole buffer would begin inside the key,
        overlap it, and be discarded — and the address would go to the tape in the clear.
        """
        found: list[re.Match[bytes]] = []
        claimed: list[tuple[int, int]] = []
        lower = buf.lower()
        for kind, pattern in _COMPILED:
            if kind not in self.kinds or not _possible(kind, lower):
                continue
            for low, high in _gaps(len(buf), claimed):
                for match in pattern.finditer(buf, low, high):
                    found.append(match)
                    claimed.append((match.start(), match.end()))
            claimed.sort()
        found.sort(key=lambda m: m.start())
        return found

    def _replace(self, buf: bytes, matches: list[re.Match[bytes]]) -> bytes:
        if not matches:
            return buf
        pieces: list[bytes] = []
        cursor = 0
        for match in matches:
            pieces.append(buf[cursor:match.start()])
            kind = self._kind_of(match)
            pieces.append(self._placeholder(kind, match.group(0)))
            self.counts[kind] = self.counts.get(kind, 0) + 1
            cursor = match.end()
        pieces.append(buf[cursor:])
        return b"".join(pieces)

    def _kind_of(self, match: re.Match[bytes]) -> str:
        for kind, pattern in _COMPILED:
            if pattern.fullmatch(match.group(0)):
                return kind
        return "secret"

    def _placeholder(self, kind: str, secret: bytes) -> bytes:
        if self.mode == "hash":
            digest = hashlib.sha256(secret).hexdigest()[:8]
            return f"[REDACTED:{kind}:{digest}]".encode()
        return f"[REDACTED:{kind}]".encode()

    def as_dict(self) -> dict[str, Any]:
        """The only thing a capsule may say about what it removed: counts by kind, never the bytes."""
        return {"mode": self.mode, "counts": dict(self.counts), "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out, "window": self.window}
