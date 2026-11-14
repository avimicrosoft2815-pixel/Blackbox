"""P0-06: config precedence (file < env), validation, and dict round-trips."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackbox import constants as C  # noqa: E402
from blackbox.config import Config, TapeConfig, load_config  # noqa: E402
from blackbox.errors import BlackBoxError  # noqa: E402


def test_defaults():
    cfg = load_config()
    assert cfg.mode == C.DEFAULT_MODE
    assert cfg.tape.segment_bytes == C.DEFAULT_SEGMENT_BYTES
    assert cfg.validate() is cfg


def test_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "blackbox.toml"
    path.write_text('[tape]\nmode = "process"\nsegment_bytes = 2097152\n', encoding="utf-8")
    monkeypatch.setenv("BLACKBOX_MODE", "kernel")
    cfg = load_config(path)
    assert cfg.mode == "kernel"
    assert cfg.tape.segment_bytes == 2097152


def test_invalid_mode_is_declared_error(monkeypatch):
    monkeypatch.setenv("BLACKBOX_MODE", "turbo")
    with pytest.raises(BlackBoxError) as exc:
        load_config()
    assert exc.value.code == "BBX-E100"


def test_segment_size_floor(monkeypatch):
    monkeypatch.setenv("BLACKBOX_SEGMENT_BYTES", "1024")
    with pytest.raises(BlackBoxError) as exc:
        load_config().validate()
    assert exc.value.code == "BBX-E130"


def test_unknown_config_key_is_ignored_for_forward_compat(tmp_path):
    path = tmp_path / "blackbox.toml"
    path.write_text('[tape]\nsegment_bytes = 1048576\nlayout = "ring"\nfuture_key = 1\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.tape.layout == "ring"
    assert not hasattr(cfg.tape, "future_key")


def test_dict_roundtrip():
    cfg = Config(mode="kernel", tape=TapeConfig(segment_bytes=8 << 20))
    assert Config.from_dict(cfg.to_dict()) == cfg
    assert cfg.validate().mode == "kernel"
