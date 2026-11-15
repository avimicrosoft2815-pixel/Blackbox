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
  without the key being recoverable. The digest is **HMAC-SHA256 under the per-machine key** (`blackbox.keys`), because
  a bare sha256 of a short secret is brute-forceable — and when the key cannot be had, the placeholder is unkeyed and
  the summary says `"keyed": false` instead of implying a key that was never used (DV-013).
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
import hmac
import os
import re
from typing import Any

from blackbox.patterns import PATTERNS, PENDING

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


_COMPILED = tuple((kind, re.compile(pattern)) for kind, pattern in PATTERNS)
_PENDING = tuple((kind, re.compile(pattern)) for kind, pattern in PENDING)

# One or two cheap literals per kind, checked with `in`: a pattern whose literal is absent cannot match, and running
# it anyway costs a scan of the whole buffer from every position. The needles are per kind rather than one big
# alternation because a twenty-branch regex is slower than twenty `in` tests on a short buffer (measured: 16 µs
# against 3 µs on 1 KiB) — regex alternation backtracks, `bytes.__contains__` does not.
_PREFILTER: dict[str, tuple[bytes, ...]] = {
    "private_key": (b"-----begin",),
    "anthropic": (b"sk-ant-",),
    "openai": (b"sk-",),
    "aws": (b"akia", b"asia"),
    "github": (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_"),
    "slack": (b"xox",),
    "jwt": (b"eyj",),
    "bearer": (b"bearer",),
    "assignment": (b"key", b"secret", b"token", b"pass"),
    "email": (b"@",),
}
# Matched against the lowercased buffer, at identical offsets (ASCII case folding is one byte for one byte), so the
# engine does not carry an `IGNORECASE` flag through every position of every pattern.
_CASE_INSENSITIVE = frozenset({"bearer", "assignment"})
_ALL_KINDS = frozenset(kind for kind, _pattern in PATTERNS)
_BIT: dict[str, int] = {kind: 1 << index for index, (kind, _pattern) in enumerate(PATTERNS)}
_ALL_BITS = (1 << len(PATTERNS)) - 1


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


def _possible(lower: bytes) -> int:
    """A bitmask of the kinds whose literal appears in this buffer.

    Straight-line `in` tests rather than a loop over a table with a generator per kind: the generator version cost
    10 µs per call, more than all ten regexes together (defect 29's second half, found by the same cost test).
    """
    mask = 0
    if b"-----begin" in lower:
        mask |= _BIT["private_key"]
    if b"sk-ant-" in lower:
        mask |= _BIT["anthropic"]
    if b"sk-" in lower:
        mask |= _BIT["openai"]
    if b"akia" in lower or b"asia" in lower:
        mask |= _BIT["aws"]
    if b"ghp_" in lower or b"gho_" in lower or b"ghu_" in lower or b"ghs_" in lower or b"ghr_" in lower:
        mask |= _BIT["github"]
    if b"xox" in lower:
        mask |= _BIT["slack"]
    if b"eyj" in lower:
        mask |= _BIT["jwt"]
    if b"bearer" in lower:
        mask |= _BIT["bearer"]
    if b"key" in lower or b"secret" in lower or b"token" in lower or b"pass" in lower:
        mask |= _BIT["assignment"]
    if b"@" in lower:
        mask |= _BIT["email"]
    return mask


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
                 kinds: tuple[str, ...] | None = None, key: bytes | None = None,
                 key_path: str | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown redaction mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.window = window
        self.kinds = kinds or tuple(kind for kind, _ in PATTERNS)
        # The key is loaded only when a pseudonym is actually going to be written, and its absence is recorded rather
        # than papered over: `keyed: false` in the summary is the difference between "correlation without plaintext"
        # and a digest a reader can brute-force.
        self.key: bytes | None = None
        self.keyed = False
        self.key_id: str | None = None
        if mode == "hash":
            self.key = key
            if self.key is None:
                try:
                    from blackbox.keys import load_or_create_key

                    self.key = load_or_create_key(key_path)
                except Exception:                          # a read-only home must not stop the recorder
                    self.key = None
            if self.key is not None:
                from blackbox.keys import key_id

                self.keyed = True
                self.key_id = key_id(self.key)
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
        lower = self._buf.lower()                       # one lowercased copy per feed, shared by both passes
        mask = _possible(lower)
        matches = self._matches(self._buf, lower, mask)
        release = self._cut(self._buf, matches, lower, mask)
        out = self._replace(self._buf[:release], [m for m in matches if m.end() <= release])
        self._buf = self._buf[release:]
        self.bytes_out += len(out)
        return out

    def _cut(self, buf: bytes, matches: list[re.Match[bytes]], lower: bytes, mask: int) -> int:
        """How much of `buf` can be written now. Everything, unless something in it is unfinished."""
        end = len(buf)
        cut = end
        for kind, pattern in _PENDING:
            if kind not in self.kinds or not mask & _BIT[kind]:
                continue
            found = pattern.search(lower if kind in _CASE_INSENSITIVE else buf)
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
        lower = buf.lower()
        out = self._replace(buf, self._matches(buf, lower, _possible(lower)))
        self.bytes_out += len(out)
        return out

    # ── internals ───────────────────────────────────────────────────────────
    def _matches(self, buf: bytes, lower: bytes, mask: int) -> list[re.Match[bytes]]:
        """Every distinct secret in `buf`, longest-first by pattern priority, none overlapping another.

        Each pattern searches only the gaps the earlier patterns left. That matters more than it looks: the private
        key's closing line `-----END RSA PRIVATE KEY-----person@host` is a legal start for the email pattern (a dash
        is a legal local-part character), so a pattern that searched the whole buffer would begin inside the key,
        overlap it, and be discarded — and the address would go to the tape in the clear.
        """
        found: list[re.Match[bytes]] = []
        claimed: list[tuple[int, int]] = []
        for kind, pattern in _COMPILED:
            if kind not in self.kinds or not mask & _BIT[kind]:
                continue
            haystack = lower if kind in _CASE_INSENSITIVE else buf
            for low, high in _gaps(len(buf), claimed):
                for match in pattern.finditer(haystack, low, high):
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
            # Sliced from `buf`, not taken from the match: a case-insensitive kind was matched against the lowercased
            # copy, and a pseudonym of the wrong case would make two distinct secrets share one.
            pieces.append(self._placeholder(kind, buf[match.start():match.end()]))
            self.counts[kind] = self.counts.get(kind, 0) + 1
            cursor = match.end()
        pieces.append(buf[cursor:])
        return b"".join(pieces)

    def _kind_of(self, match: re.Match[bytes]) -> str:
        text = match.group(0)
        for kind, pattern in _COMPILED:
            if pattern.fullmatch(text) or pattern.fullmatch(text.lower()):
                return kind
        return "secret"

    def _placeholder(self, kind: str, secret: bytes) -> bytes:
        if self.mode == "hash":
            return f"[REDACTED:{kind}:{self._pseudonym(secret)}]".encode()
        return f"[REDACTED:{kind}]".encode()

    def _pseudonym(self, text: bytes) -> str:
        """The 8 hex characters in `[REDACTED:<kind>:…]`: an HMAC when a key was available, a digest when not."""
        if self.keyed and self.key is not None:
            return hmac.new(self.key, text, hashlib.sha256).hexdigest()[:8]
        return hashlib.sha256(text).hexdigest()[:8]

    def as_dict(self) -> dict[str, Any]:
        """The only thing a capsule may say about what it removed: counts by kind, never the bytes."""
        return {"mode": self.mode, "counts": dict(self.counts), "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out, "window": self.window,
                # Which key produced the pseudonyms — and whether one did at all. A reader who sees `keyed: false`
                # knows the digest is brute-forceable for a short value; one who sees a `key_id` knows which key to
                # delete if it needs rotating.
                "keyed": self.keyed, "key_id": self.key_id}
