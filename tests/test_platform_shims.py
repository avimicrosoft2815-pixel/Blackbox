"""P2-03/P2-04: the Windows and macOS shims exist, are honest about what they cannot do, and never raise.

They cannot be *run* on this machine (Linux), so what is tested here is the contract every shim must keep:
the same function names as `posix`, a `capabilities()` dict that admits it was not executed, no claim of
durability it cannot make, and no exception escaping a probe that the recorder calls on a hot path.
"""
from __future__ import annotations

import os

import pytest

from blackbox.platform import capabilities, current, macos, posix, windows

COMMON = ("proc_start_ticks", "pid_alive", "boot_id", "fsync_dir", "fdatasync", "fs_type",
          "is_network_fs", "is_regular_file", "capabilities")


@pytest.mark.parametrize("mod", [posix, windows, macos])
def test_every_shim_answers_the_same_questions(mod):
    for name in COMMON:
        assert callable(getattr(mod, name)), f"{mod.__name__} is missing {name}"


@pytest.mark.parametrize("mod", [windows, macos])
def test_a_shim_that_was_not_executed_here_says_so(mod):
    caps = mod.capabilities()
    assert caps["tested_here"] is False
    assert "not executed in this environment" in caps["note"]
    assert caps["prealloc"] == "zero-fill", "a sparse segment must never be mapped"


@pytest.mark.parametrize("mod", [windows, macos])
def test_unsupported_questions_answer_none_rather_than_guessing(mod):
    assert mod.proc_start_ticks(os.getpid()) is None
    assert mod.boot_id() is None
    assert mod.fs_type(".") == "unknown"


@pytest.mark.parametrize("mod", [windows, macos])
def test_probes_never_raise_and_tolerate_a_bad_path(mod):
    assert isinstance(mod.pid_alive(os.getpid()), bool)
    assert mod.pid_alive(-1) is False
    assert mod.is_regular_file("/definitely/not/here") is False
    mod.fsync_dir("/definitely/not/here")            # best effort: must not raise


def test_current_picks_a_shim_and_reports_capabilities():
    mod = current()
    assert mod in (posix, windows, macos)
    caps = capabilities()
    assert "platform" in caps and "prealloc" in caps


def test_the_posix_shim_is_the_one_tested_here():
    caps = capabilities()
    assert caps["tested_here"] is True


def test_macos_directory_fsync_is_a_no_op_by_contract(tmp_path):
    assert macos.DIRECTORY_FSYNC is False
    macos.fsync_dir(str(tmp_path))                    # EINVAL on APFS must be swallowed, not raised
