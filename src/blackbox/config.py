"""Configuration (P0-12): frozen dataclasses, `blackbox.toml`, `BLACKBOX_*` env, validation.

Precedence: explicit kwargs > `BLACKBOX_*` env > `./blackbox.toml` > `~/.blackbox/config.toml` > defaults.
Zero dependencies: TOML is read with stdlib `tomllib` (this is why the Python floor is 3.11).
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from blackbox import constants as C
from blackbox.errors import bbx


@dataclass(frozen=True)
class TapeConfig:
    segment_bytes: int = C.DEFAULT_SEGMENT_BYTES
    max_bytes: int = C.DEFAULT_TAPE_MAX_BYTES
    layout: str = "segments"


@dataclass(frozen=True)
class RetentionConfig:
    keep_days: int = 30
    keep_capsules: int = 50
    max_bytes: int = 1 << 30
    gc_grace_h: int = 24


@dataclass(frozen=True)
class Config:
    project: str = "default"
    data_dir: str = "~/.blackbox"
    mode: str = C.DEFAULT_MODE
    content: str = C.DEFAULT_CONTENT
    sync_ms: int = C.DEFAULT_SYNC_MS
    sync_bytes: int = C.DEFAULT_SYNC_BYTES
    blob_inline: int = C.DEFAULT_BLOB_INLINE
    blob_max: int = C.DEFAULT_BLOB_MAX
    queue_max: int = C.DEFAULT_QUEUE_MAX
    sample: float = C.DEFAULT_SAMPLE
    inherit_threads: bool = False
    tape: TapeConfig = field(default_factory=TapeConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)

    # ── validation ──────────────────────────────────────────────────────────
    def validate(self) -> Config:
        if self.mode not in C.MODES:
            raise bbx("BBX-E100", f"mode must be one of {C.MODES}")
        if self.content not in C.CONTENTS:
            raise bbx("BBX-E100", f"content must be one of {C.CONTENTS}")
        if self.tape.segment_bytes < C.MIN_SEGMENT_BYTES:
            raise bbx("BBX-E130", f"segment_bytes {self.tape.segment_bytes} < {C.MIN_SEGMENT_BYTES}")
        # a P0 event (frame header + trailer + ~0 payload) must fit with room for SEG_END
        if self.tape.segment_bytes - C.SEGMENT_HEADER_BYTES - C.RESERVED_HEAD_ROOM < C.FRAME_OVERHEAD_BYTES:
            raise bbx("BBX-E130", "segment too small for one frame")
        if not 0.0 < self.sample <= 1.0:
            raise bbx("BBX-E100", "sample must be in (0, 1]")
        if self.sync_ms <= 0 or self.sync_bytes <= 0:
            raise bbx("BBX-E100", "sync_ms and sync_bytes must be positive")
        if self.blob_inline < 0 or self.blob_max < self.blob_inline:
            raise bbx("BBX-E100", "blob_max must be >= blob_inline")
        return self

    # ── (de)serialisation: round-trip is a property test (P0-12) ─────────────
    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = {k: v for k, v in data.items() if k not in known}
        if unknown:
            # forward compatibility: unknown keys are a warning, never an error (Appendix A)
            data = {k: v for k, v in data.items() if k in known}
        tape_raw = data.get("tape") or {}
        ret_raw = data.get("retention") or {}
        tape = TapeConfig(**{k: v for k, v in tape_raw.items() if k in {f.name for f in dataclasses.fields(TapeConfig)}})
        ret = RetentionConfig(**{k: v for k, v in ret_raw.items() if k in {f.name for f in dataclasses.fields(RetentionConfig)}})
        simple = {k: v for k, v in data.items() if k not in ("tape", "retention")}
        return cls(tape=tape, retention=ret, **simple)

    def resolve_data_dir(self) -> Path:
        return Path(os.path.expanduser(self.data_dir)).resolve()


# ── loading ─────────────────────────────────────────────────────────────────
_ENV_MAP = {
    "BLACKBOX_DATA_DIR": ("data_dir", str),
    "BLACKBOX_MODE": ("mode", str),
    "BLACKBOX_CONTENT": ("content", str),
    "BLACKBOX_PROJECT": ("project", str),
    "BLACKBOX_SYNC_MS": ("sync_ms", int),
    "BLACKBOX_SYNC_BYTES": ("sync_bytes", int),
    "BLACKBOX_SEGMENT_BYTES": ("tape.segment_bytes", int),
    "BLACKBOX_SAMPLE": ("sample", float),
}


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise bbx("BBX-E100", f"invalid TOML in {path}: {exc}") from exc


def _flatten_document(doc: dict) -> dict:
    """Map Appendix A TOML sections onto Config fields."""
    out: dict = {}
    core = doc.get("core", {})
    for key in ("project", "data_dir", "mode", "content", "sync_ms", "sync_bytes",
                "blob_inline", "blob_max", "queue_max", "sample", "inherit_threads"):
        if key in core:
            out[key] = core[key]
    tape = doc.get("tape", {})
    if "segment_bytes" in tape:
        out.setdefault("tape", {})["segment_bytes"] = tape["segment_bytes"]
    if "max_bytes" in tape:
        out.setdefault("tape", {})["max_bytes"] = tape["max_bytes"]
    ret = doc.get("retention", {})
    if ret:
        out.setdefault("retention", {}).update({k: v for k, v in ret.items()
                                                if k in {f.name for f in dataclasses.fields(RetentionConfig)}})
    # top-level [tape]/[retention] forms are also accepted (Appendix A' delta)
    for section, target in (("tape", "tape"), ("retention", "retention")):
        if section in doc and isinstance(doc[section], dict):
            merged = out.setdefault(target, {})
            for k, v in doc[section].items():
                merged.setdefault(k, v)
    return out


def _merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_dotted(data: dict, dotted: str, value) -> None:
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        data.setdefault(head, {})[tail] = value
    else:
        data[dotted] = value


def load_config(path: str | os.PathLike | None = None,
                overrides: dict | None = None,
                env: dict | None = None) -> Config:
    """Build a validated Config from defaults ← user TOML ← project TOML ← env ← explicit overrides."""
    env = dict(os.environ if env is None else env)
    user_toml = _load_toml(Path(os.path.expanduser("~/.blackbox/config.toml")))
    project_toml = _load_toml(Path(path) if path else Path("blackbox.toml"))

    data = _merge(_flatten_document(user_toml), _flatten_document(project_toml))
    for var, (dotted, caster) in _ENV_MAP.items():
        if var in env:
            try:
                _set_dotted(data, dotted, caster(env[var]))
            except (TypeError, ValueError) as exc:
                raise bbx("BBX-E100", f"bad value for {var}: {env[var]!r}") from exc
    for key, value in (overrides or {}).items():
        _set_dotted(data, key, value)

    return Config.from_dict(data).validate()
