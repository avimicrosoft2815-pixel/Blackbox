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
    for name in (*COMMON, "pwrite", "pread", "is_current"):
        assert callable(getattr(mod, name)), f"{mod.__name__} is missing {name}"


NON_CURRENT = [m for m in (posix, windows, macos) if m is not current()]


@pytest.mark.parametrize("mod", NON_CURRENT)
def test_a_shim_that_is_not_this_machine_says_so(mod):
    """C22: `tested_here` was a constant, so the macOS cell failed on its own honest line."""
    caps = mod.capabilities()
    assert caps["is_current"] is False, f"{mod.NAME} must not claim to be the current platform"
    assert "not executed in this environment" in caps["note"]
    assert caps["prealloc"] == "zero-fill", "a sparse segment must never be mapped"


@pytest.mark.parametrize("mod", NON_CURRENT)
def test_unsupported_questions_answer_none_rather_than_guessing(mod):
    assert mod.proc_start_ticks(os.getpid()) is None
    assert mod.boot_id() is None
    assert mod.fs_type(".") == "unknown"


@pytest.mark.parametrize("mod", [windows, macos])
def test_probes_never_raise_and_tolerate_a_bad_path(mod):
    """Both non-POSIX shims: a probe on a path that does not exist must answer, not raise."""
    assert isinstance(mod.pid_alive(os.getpid()), bool)
    assert mod.pid_alive(-1) is False
    assert mod.is_regular_file("/definitely/not/here") is False
    mod.fsync_dir("/definitely/not/here")            # best effort: must not raise


def test_current_picks_a_shim_and_reports_capabilities():
    mod = current()
    assert mod in (posix, windows, macos)
    caps = capabilities()
    assert "platform" in caps and "prealloc" in caps


def test_exactly_one_shim_reports_itself_as_current():
    flags = [m.capabilities()["is_current"] for m in (posix, windows, macos)]
    assert flags.count(True) == 1, "one machine, one current shim"
    assert capabilities()["is_current"] is True
    assert capabilities()["platform"] == current().NAME


def test_positional_io_is_identical_across_shims(tmp_path):
    """The writer calls `pwrite`/`pread`; every shim must implement them with the same semantics."""
    data = bytearray(b"\x00" * 32)
    for mod in (posix, windows, macos):
        path = tmp_path / f"{mod.NAME}.bin"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.ftruncate(fd, 32)
            mod.pwrite(fd, b"hello", 8)
            assert mod.pread(fd, 5, 8) == b"hello", f"{mod.NAME}.pread must agree with {mod.NAME}.pwrite"
            assert mod.pread(fd, 5, 0) == b"\x00" * 5
        finally:
            os.close(fd)
    assert data


def test_macos_directory_fsync_is_a_no_op_by_contract(tmp_path):
    assert macos.DIRECTORY_FSYNC is False
    macos.fsync_dir(str(tmp_path))                    # EINVAL on APFS must be swallowed, not raised


# ── P2-03 (C20): the lease's start-time check is OpenProcess + GetProcessTimes, not a guess ──────────────
class _FakeFiletime:
    """One FILETIME: two u32 halves. The shim only reads them, so the test can hand it any value."""

    def __init__(self, high: int = 0, low: int = 0) -> None:
        self.dwHighDateTime = high
        self.dwLowDateTime = low


class _FakeKernel32:
    def __init__(self, *, opens: bool = True, times: bool = True, high: int = 0x01D0, low: int = 0x1234):
        self.opens, self.times, self.high, self.low = opens, times, high, low
        self.closed: list[object] = []
        self.argtypes: dict[str, object] = {}

    def OpenProcess(self, access, inherit, pid):
        assert access == windows.PROCESS_QUERY_LIMITED_INFORMATION
        assert inherit is False
        return 0xDEAD if self.opens else 0

    def GetProcessTimes(self, handle, created, exited, kernel_t, user_t):
        if not self.times:
            return 0
        created.dwHighDateTime, created.dwLowDateTime = self.high, self.low
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def _install_fake_kernel(monkeypatch, fake: _FakeKernel32) -> None:
    """Feed the shim a fake `kernel32` through the one seam it has: its own loader.

    Everything below that seam (argument marshalling, the FILETIME halves, handle lifetime) is the real code
    path. `ctypes` itself is imported lazily inside the loader, so on Linux the loader is replaced rather than
    `ctypes.WinDLL` — which is what a Windows run would resolve.
    """
    monkeypatch.setattr(windows, "_kernel32", lambda: (fake, _FakeFiletime))


def test_windows_start_time_comes_from_GetProcessTimes(monkeypatch):
    fake = _FakeKernel32(high=0x01D0, low=0x1234)
    _install_fake_kernel(monkeypatch, fake)
    ticks = windows.proc_start_ticks(4321)
    assert ticks == (0x01D0 << 32) | 0x1234, "the creation time is the FILETIME the API returned"
    assert fake.closed == [0xDEAD], "the handle is closed on every path"


@pytest.mark.parametrize("kwargs,closed", [({"opens": False}, []), ({"times": False}, [0xDEAD])])
def test_a_failed_win32_call_reports_no_token_rather_than_a_wrong_one(monkeypatch, kwargs, closed):
    fake = _FakeKernel32(**kwargs)
    _install_fake_kernel(monkeypatch, fake)
    assert windows.proc_start_ticks(4321) is None, "cannot prove liveness must be None, never a fake tick"
    # a handle that was opened is closed even when the second call fails: no leak on the error path
    assert fake.closed == closed


def test_without_a_usable_kernel32_the_shim_reports_no_token(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no kernel32 here")

    monkeypatch.setattr(windows, "_kernel32", boom)
    assert windows.proc_start_ticks(1) is None, "no token must never be faked"
    assert windows.proc_start_ticks(0) is None
    assert windows.capabilities()["process_start_token"] is True


def test_macos_uses_fullfsync_when_the_constant_exists(monkeypatch):
    if macos.fcntl is None:
        pytest.skip("fcntl is POSIX-only; this module must still import on Windows")
    issued: list[tuple[int, int]] = []
    monkeypatch.setattr(macos.fcntl, "fcntl", lambda fd, cmd: issued.append((fd, cmd)), raising=False)
    monkeypatch.setattr(macos.fcntl, "F_FULLFSYNC", 51, raising=False)
    macos.fdatasync(7)
    assert issued == [(7, 51)], "kernel/paranoid must reach the device, not just the drive cache"
