"""S17 / P1-20 (C5): ENOSPC injected at preallocation and at the zero-fill write.

The claim under test is narrow and honest: with the fault **injected**, the recorder degrades, the app's
stdout is byte-identical to the run without the fault, and the process exits 0. The sparse-file SIGBUS
control case needs a genuinely full filesystem (`mount -t tmpfs -o size=2m`), which this environment refuses
(no superuser), so it is labelled `not testable in this environment` in docs/STATUS.md.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox import constants as C
from blackbox.tape import prealloc
from blackbox.tape.writer import TapeWriter

CHILD = textwrap.dedent(
    """
import hashlib as _hashlib
import sys

sys.path.insert(0, {src!r})
from blackbox import constants as C
from blackbox.tape.writer import TapeWriter

mode, data_dir, fault = sys.argv[1], sys.argv[2], sys.argv[3]
# the fault is armed *after* open(): an ENOSPC at first open is a legitimate hard error (nothing is
# recordable at all); S17 is about a roll that cannot get another segment
w = TapeWriter(data_dir, segment_bytes=64 * 1024, mode=mode).open()
if fault == "posix_fallocate":
    import blackbox.tape.prealloc as prealloc

    def boom(fd, off, length):
        raise OSError(28, "injected ENOSPC in posix_fallocate")

    prealloc.os.posix_fallocate = boom

app = _hashlib.blake2b(digest_size=16)
for i in range(400):
    payload = ("event-%03d" % i).encode() * 8
    app.update(payload)                       # what the *app* did, independent of the tape
    w.emit(C.KIND_MODEL_REQUEST, payload)
    if fault == "zero-fill" and i == 200:
        import blackbox.tape.prealloc as prealloc2

        def zero_boom(fd, data):
            raise OSError(28, "injected ENOSPC in the zero-fill write")

        prealloc2.os.write = zero_boom
    if not w._mm:
        break
w.flush()
# the app's own stdout carries nothing about the tape, so it must be byte-identical with and without the
# injected fault; the tape's own view goes to a side file for the assertions that need it
with open(data_dir + "/writer-diag.txt", "w", encoding="utf-8") as fh:
    fh.write("seq=%d dropped=%d degraded=%s\\n" % (w.head.head_seq, w.dropped_events,
                                                   w.degraded_reason or "none"))
w.close()
print("events=%d app=%s" % (400, app.hexdigest()))
"""
)


CHILD_RUNS = itertools.count()


def run_child(tmp_path: Path, mode: str, fault: str) -> subprocess.CompletedProcess:
    d = Path(tempfile.mkdtemp(dir=tmp_path, prefix=f"{CHILD_RUNS}-{mode}-{fault}-"))
    script = CHILD.format(src=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-c", script, mode, str(d), fault],
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")})


@pytest.mark.parametrize("mode", ["process", "kernel"])
def test_app_survives_enospc_at_the_first_segment(tmp_path, mode):
    """S17 also covers the first preallocation: a recorder that cannot start is a sink, not a crash."""
    d = Path(tempfile.mkdtemp(dir=tmp_path, prefix="first-seg-"))
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from blackbox import constants as C
        import blackbox.tape.prealloc as prealloc

        def boom(path, size):
            raise prealloc.PreallocError(28, "injected ENOSPC at the first preallocation")

        prealloc.preallocate = boom
        from blackbox.tape.writer import TapeWriter

        w = TapeWriter({str(d)!r}, segment_bytes=64 * 1024, mode={mode!r}).open()
        for i in range(50):
            w.emit(C.KIND_MODEL_REQUEST, b"x")
        w.flush()
        w.close()
        print("events=50 recording=%s degraded=%s dropped=%d" % (w.recording, w.degraded_reason, w.dropped_events))
        """
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       timeout=180, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert "recording=False" in r.stdout and "degraded=NO_SPACE" in r.stdout and "dropped=50" in r.stdout


@pytest.mark.parametrize("mode", ["process", "kernel"])
@pytest.mark.parametrize("fault", ["none", "posix_fallocate"])
def test_app_survives_enospc_at_preallocation(tmp_path, mode, fault):
    base = run_child(tmp_path, mode, "none")
    assert base.returncode == 0, base.stderr
    injected = run_child(tmp_path, mode, fault)
    assert injected.returncode == 0, injected.stderr
    assert injected.stdout == base.stdout, "the app's own output must not change"
    assert injected.stderr == ""


@pytest.mark.parametrize("mode", ["process", "kernel"])
def test_app_survives_enospc_in_the_zero_fill_write(tmp_path, mode):
    r = run_child(tmp_path, mode, "zero-fill")
    assert r.returncode == 0, r.stderr
    assert r.returncode == 0 and "events=400 app=" in r.stdout


def test_degraded_is_recorded_on_the_tape(tmp_path, monkeypatch):
    """The tape, not just the writer object, must show the degradation to a later reader."""
    from blackbox.tape import scanner

    w = TapeWriter(str(tmp_path), segment_bytes=64 * 1024).open()
    for i in range(40):
        w.emit(C.KIND_MODEL_REQUEST, b"x" * 200)
    monkeypatch.setenv(prealloc.FAULT_ENV, "ENOSPC")
    assert w.roll() is False
    monkeypatch.delenv(prealloc.FAULT_ENV)
    uuid = w.writer_uuid.hex()
    w.flush()
    scan = scanner.scan(str(tmp_path), uuid)
    kinds = {f["kind"] for f in scan.frames}
    assert C.KIND_DEGRADED in kinds
    flagged = scan.head and (scan.head.flags & C.HEAD_FLAG_DEGRADED_NO_SPACE)
    assert flagged
    w.close()
