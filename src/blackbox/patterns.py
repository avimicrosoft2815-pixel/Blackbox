"""The redaction patterns (P3-11, §12.2): what a credential looks like, and what it looks like while arriving.

Split out of `redact.py` because this file is the part a security reviewer reads: the rules, in one place, with the
ordering that matters written down. The state machine that applies them — the hold, the cut, the overlap resolution —
is in `redact.py` and has no opinions about what a secret is.
"""
from __future__ import annotations

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
    ("bearer", rb"bearer\s+[A-Za-z0-9._\-]{16,}"),
    ("assignment", rb"(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\"?\s*[:=]\s*\"?"
                   rb"[A-Za-z0-9._\-+/=]{12,}"),
    ("email", rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
)

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
    ("bearer", rb"bearer\s*[A-Za-z0-9._\-]*\Z"),
    ("assignment", rb"(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\"?\s*"
                   rb"(?:[:=]\s*\"?[A-Za-z0-9._\-+/=]*)?\Z"),
    ("email", rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]*\Z"),
)

