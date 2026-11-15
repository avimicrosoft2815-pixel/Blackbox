"""Full preallocation and sparse-file refusal (P1-20, plan §8.6 step 2).

A sparse mapping that faults under `ENOSPC`/EOF raises `SIGBUS` **in the faulting process** — i.e. in the
user's application, which would be a fail-open violation. We therefore preallocate every segment and treat
allocation failure as a degradable condition, never as an exception into user code.

`SIGBUS` is *not* claimed here: `tests/test_prealloc_sigbus.py` (stage S17) is the test that must deliver
`SIGBUS` in the sparse control case and must not deliver it on the preallocated path.
"""
from __future__ import annotations

import os
import subprocess

from blackbox.errors import bbx
from blackbox.platform import current as _plat_current

_plat = _plat_current()

FAULT_ENV = "BLACKBOX_FAULT_PREALLOC"       # test hook: values "ENOSPC", "EACCES"


class PreallocError(OSError):
    """Raised internally; the writer converts it into DEGRADED_NO_SPACE."""


def _du_kib(path: str) -> int:
    try:
        # fixed argv, no shell
        out = subprocess.run(["du", "-k", path], capture_output=True, text=True, timeout=10)  # noqa: S603,S607
        return int(out.stdout.split()[0])
    except Exception:
        return -1


def is_sparse(path: str, size: int) -> bool:
    """True when the file is allocated lazily (fewer blocks than bytes). Best effort per platform.

    `st_blocks` is POSIX-only, and on NTFS the question does not even have the same answer: `SetEndOfFile`
    (what `os.ftruncate` becomes) allocates the clusters, and a file becomes sparse only after
    `FSCTL_SET_SPARSE`. So Windows returns False — not because it cannot measure, but because a file created by
    `open_fd` + `ftruncate` there really is dense, which is what S17 wants."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size < size:
        return True
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return False                     # Windows: no st_blocks; treat as allocated
    return bool(blocks * 512 < size * 0.9)


def preallocate(path: str, size: int) -> str:
    """Fully allocate `path` to `size` bytes. Returns the method used. Raises PreallocError on failure."""
    fault = os.environ.get(FAULT_ENV)
    if fault == "ENOSPC":
        raise PreallocError(28, "injected ENOSPC at preallocation")
    if fault == "EACCES":
        raise PreallocError(13, "injected EACCES at preallocation")

    fd = _plat.open_fd(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "posix_fallocate"):
            try:
                os.posix_fallocate(fd, 0, size)
                return "posix_fallocate"
            except OSError as exc:
                if exc.errno in (95, 38):          # ENOTSUP / ENOSYS -> fall back to zero-fill
                    pass
                elif exc.errno in (28, 122):       # ENOSPC / EDQUOT
                    raise PreallocError(exc.errno, "no space for segment preallocation") from exc
                else:
                    raise
        chunk = b"\x00" * min(1 << 20, size)
        written = 0
        while written < size:
            n = min(len(chunk), size - written)
            os.write(fd, chunk[:n])
            written += n
        os.fsync(fd)
        return "write-zeros"
    finally:
        os.close(fd)


def ensure_not_sparse(path: str, size: int) -> None:
    if is_sparse(path, size):
        raise bbx("BBX-E131", f"{path} is sparse after preallocation")
