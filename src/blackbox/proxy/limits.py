"""The proxy's real concurrency limit is the process's descriptor table (defect 62).

Every request the proxy serves holds **two** descriptors — the client's socket and the one to the upstream — so a soft
limit of 1024 (this machine's default) is a ceiling of about 500 requests in flight, and a macOS shell's default soft
limit of 256 is a ceiling of about 120, *below* §15.1's 200 concurrent streams. Past the ceiling the loop's `accept`
fails (`socket.accept() out of system resource`, `EMFILE`), asyncio logs a traceback per attempt, and a child's next
request simply waits: measured on this box at 450 concurrent requests, a probe issued during the burst timed out after
4 s and was answered in 2 ms once the burst's sockets closed.

Two honest responses, and only one of them belongs here:

* **use the budget the OS already grants** — the hard limit is 524 288 on this box and every POSIX host allows the soft
  limit to be raised to it; that is not a policy decision, it is reading a number the kernel already published. That is
  this module.
* **cap concurrency and choose what to answer past the cap** — 503 with a reason, a queue with a timeout, or nothing at
  all. That *is* a policy: refusing a request the upstream would have served breaks the pass-through promise this proxy
  exists for, so the choice is recorded in `docs/defects.md` (defect 62) for the owner rather than taken here.

Nothing is claimed on Windows: there is no `RLIMIT_NOFILE` and the handle table grows with the process.
"""
from __future__ import annotations

import contextlib

# Ask for this much and no more. The hard limit can be `RLIM_INFINITY` (2**63-1 on some kernels), and `setrlimit` answers
# `EPERM` for a value the kernel cannot honour, so an infinite budget is read as "up to a number large enough for a
# recorder and small enough to fail predictably".
DEFAULT_TARGET = 65_536


def raise_fd_limit(target: int = DEFAULT_TARGET) -> tuple[int, int] | None:
    """Raise this process's soft descriptor limit toward `target`, bounded by the hard limit.

    Returns `(before, after)` in descriptors, or `None` where the platform has no such limit (Windows). Never raises: a
    proxy that refuses to start because it could not get more descriptors would be worse than one that starts with the
    budget it had (the same rule as every other fail-open path in this project).
    """
    try:
        import resource
    except ImportError:                                   # Windows: no RLIMIT_NOFILE, nothing to read or raise
        return None
    with contextlib.suppress(Exception):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        before = int(soft)                                # `RLIM_INFINITY` is -1 on every platform that has it
        if soft == resource.RLIM_INFINITY:
            return (before, before)                        # already unlimited: nothing to raise
        wanted = target if hard == resource.RLIM_INFINITY else min(int(hard), target)
        if wanted <= before:
            return (before, before)                        # already at or above what was asked for
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (ValueError, OSError):
            return (before, before)                        # the OS said no; the proxy runs with what it had
        return (before, int(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))
    return None


def fd_budget() -> int | None:
    """The descriptors this process may hold *right now*, or `None` where the platform has no such budget.

    Read, not assumed, and read **after** `raise_fd_limit()` has had its chance: the caps in `proxy/server.py` are derived
    from this number, so a host that refused the raise gets smaller caps instead of a documented 808 that silently exceeds
    the 256 a macOS shell starts with. `None` is the honest answer for Windows (no `RLIMIT_NOFILE`; the handle table is
    bounded by memory rather than by a number a process can read) and for an already-unlimited soft limit.
    """
    try:
        import resource
    except ImportError:                                   # Windows
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft == resource.RLIM_INFINITY or int(soft) <= 0:
        return None
    return int(soft)


__all__ = ["DEFAULT_TARGET", "fd_budget", "raise_fd_limit"]
