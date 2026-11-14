"""Typed error registry (P0-11). Every code used in code must be declared here."""
from __future__ import annotations

# code -> (short message, remediation shown to the user)
REGISTRY: dict[str, tuple[str, str]] = {
    "BBX-E100": ("capture hook install failed", "run `blackbox doctor --capture`; recording continues in safe mode"),
    "BBX-E120": ("unsupported filesystem for mmap mode", "move the data dir to a local disk or use layout=ring"),
    "BBX-E130": ("segment too small for the configured frame", "raise segment_bytes (minimum 65536)"),
    "BBX-E131": ("refusing to write a sparse segment", "check free space and filesystem; see docs/format.md §5"),
    "BBX-E140": ("segment preallocation failed", "free disk space; the writer degrades instead of raising"),
    "BBX-E150": ("retention blocked by a pin", "run `blackbox ls --pinned` or raise the budget"),
    "BBX-E151": ("invalid CBOR payload", "a producer wrote a non-canonical or malformed payload"),
    "BBX-E152": ("codec unavailable for a recorded body", "install the optional codec extra or re-record with content=hash"),
    "BBX-E210": ("writer is alive; recovery refused", "stop the process or pass --force-if-dead"),
    "BBX-E211": ("lease held by a reused pid (start time mismatch)", "run `blackbox doctor --locks`"),
    "BBX-E220": ("both head slots invalid", "run `blackbox recover --full-scan`"),
    "BBX-E221": ("tape format newer than this reader", "upgrade blackbox"),
    "BBX-E222": ("meta checksum mismatch", "quarantine the tape directory; do not delete it"),
    "BBX-E223": ("chain break", "run `blackbox verify` for the first discontinuity"),
    "BBX-E224": ("sealed segment does not match its footer", "quarantine the segment; do not delete it"),
    "BBX-E225": ("frame is larger than one segment", "externalise the payload or raise segment_bytes"),
    "BBX-E310": ("requested window exceeds max_attached shards", "narrow the range or read capsules"),
    "BBX-E330": ("index schema newer than this binary", "upgrade, or run `blackbox index --rebuild`"),
    "BBX-E401": ("capsule too large", "raise capsule_max_bytes or split the run"),
    "BBX-E402": ("not a readable capsule archive", "the file is truncated or not a .bbx"),
    "BBX-E403": ("unsafe path inside capsule", "the capsule is hostile; do not extract it"),
    "BBX-E404": ("duplicate entry names in capsule", "the capsule is hostile; do not extract it"),
    "BBX-E405": ("capsule decompression ratio over limit", "the capsule is hostile; do not extract it"),
    "BBX-E406": ("encrypted or unknown-method capsule entry", "unsupported capsule"),
    "BBX-E407": ("capsule manifest invalid or from a newer format", "upgrade blackbox"),
    "BBX-E408": ("capsule entry hash does not verify", "the capsule was modified or corrupted"),
    "BBX-E409": ("capsule extraction exceeded the declared size", "the capsule is hostile"),
    "BBX-E410": ("malformed capsule part", "the capsule is damaged"),
    "BBX-E411": ("generated test failed the safety scan", "review the test before running it"),
    "BBX-E412": ("derived capsule part drifted from the raw frames", "regenerate the capsule"),
    "BBX-E510": ("network blocked during replay", "remove the call or pass --allow-network"),
    "BBX-E520": ("replay divergence in exact mode", "read the diff; this is the feature working"),
    "BBX-E530": ("cassette exhausted", "re-record or use --mode loose"),
}


class BlackBoxError(Exception):
    """Base error. Carries a registry code; never raised into user code (see §11.6)."""

    def __init__(self, code: str, detail: str = ""):
        if code not in REGISTRY:
            raise KeyError(f"undeclared error code {code!r}")  # programming error, fails tests
        self.code = code
        self.detail = detail
        msg, fix = REGISTRY[code]
        super().__init__(f"{code}: {msg}" + (f" [{detail}]" if detail else "") + f" — {fix}")

    def to_dict(self) -> dict:
        msg, fix = REGISTRY[self.code]
        return {"code": self.code, "message": msg, "detail": self.detail, "remediation": fix}


def bbx(code: str, detail: str = "") -> BlackBoxError:
    return BlackBoxError(code, detail)
