"""The proxy's descriptor budget (defect 62): every request holds two descriptors, so the table is the concurrency limit.

Measured on this box before the fix: with the default soft limit of 1024, 450 concurrent in-flight requests filled the
table, the loop's `accept` failed with `socket.accept() out of system resource`, and a probe issued during the burst
timed out after **4 s** — the child whose prompt is waiting gets nothing to read. After raising the soft limit to what
the OS already granted (65536 here), the same burst is served and that probe answers in **2 ms**.

The tests below are about the arithmetic, not about a benchmark: a limit too small for the connections the test opens,
and an assertion that they are served anyway.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.proxy.limits import DEFAULT_TARGET, fd_budget, raise_fd_limit

#: Windows has no `RLIMIT_NOFILE` and no soft/hard descriptor budget, so the two tests that *measure* one are POSIX
#: tests: they would otherwise assert the absence of a mechanism rather than a bug. The Windows behaviour (`None`, no
#: exception) is covered by `test_a_platform_without_the_limit_says_so_and_does_not_raise`, which runs everywhere.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="no RLIMIT_NOFILE on Windows")


@posix_only
def test_the_soft_limit_is_raised_to_what_the_kernel_already_grants() -> None:
    """Run in a subprocess: lowering a soft limit is allowed, but a test must not leave the suite with a bigger budget
    than it started with (and a raise cannot be undone where the hard limit is the ceiling)."""
    probe = textwrap.dedent("""
        import json, resource, sys
        sys.path.insert(0, "src")
        from blackbox.proxy.limits import raise_fd_limit
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 256), hard))
        before = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        answer = raise_fd_limit(target=4096)
        after = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        print(json.dumps({"before": before, "answer": answer, "after": after,
                          "hard": hard if hard != resource.RLIM_INFINITY else -1}))
    """)
    finished = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert finished.returncode == 0, finished.stderr[-800:]
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answer["before"] == 256
    assert answer["answer"] is not None
    assert answer["after"] > answer["before"], answer
    assert answer["after"] == min(4096, answer["hard"]) if answer["hard"] > 0 else True
    assert answer["answer"][0] == 256 and answer["answer"][1] == answer["after"]


def test_a_platform_without_the_limit_says_so_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no `RLIMIT_NOFILE`; the answer is `None`, not an exception in the middle of starting a proxy."""
    monkeypatch.setitem(sys.modules, "resource", None)          # `import resource` raises ImportError
    assert raise_fd_limit() is None


@posix_only
def test_a_burst_larger_than_the_old_soft_limit_is_still_served() -> None:
    """The arithmetic of defect 62, as a test: 40 concurrent requests need ~80 descriptors, so a proxy whose soft limit
    is 64 cannot serve them unless it raises the limit first. The probe in the middle of the burst is the child's side.
    """
    burst = textwrap.dedent("""
        import json, resource, socket, sys, threading, time
        sys.path.insert(0, "src")
        from blackbox.proxy.limits import raise_fd_limit
        from blackbox.proxy.routes import Route, Routes
        from blackbox.proxy.supervisor import ProxyThread

        # A tiny budget first: this is the macOS-shell case (soft 256) taken to its extreme, not a hypothetical.
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))

        sink = socket.socket(); sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sink.bind(("127.0.0.1", 0)); sink.listen(256)
        held = []
        def accept_all():
            while True:
                try:
                    held.append(sink.accept()[0])
                except OSError:
                    return
        threading.Thread(target=accept_all, daemon=True).start()

        proxy = ProxyThread(Routes([Route(name="openai", base=f"http://127.0.0.1:{sink.getsockname()[1]}")],
                                   default="openai"))
        proxy.start()
        result = {"fd_limit": proxy.fd_limit, "soft_after": resource.getrlimit(resource.RLIMIT_NOFILE)[0]}

        clients = []
        for _ in range(40):
            s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
            s.sendall(b"POST /v1/chat HTTP/1.1\\r\\nHost: x\\r\\nContent-Length: 2\\r\\n\\r\\nhi")
            clients.append(s)
        time.sleep(0.5)
        result["upstream_holds"] = len(held)

        probe = socket.socket(); probe.settimeout(5)
        try:
            probe.connect(("127.0.0.1", proxy.port))
            probe.sendall(b"GET /v1/models HTTP/1.1\\r\\nHost: x\\r\\nConnection: close\\r\\n\\r\\n")
            result["probe"] = "served"
        except OSError as exc:
            result["probe"] = f"{type(exc).__name__}: {exc}"
        finally:
            probe.close()
        for s in clients:
            s.close()
        proxy.stop()
        print(json.dumps(result))
    """)
    finished = subprocess.run([sys.executable, "-c", burst], cwd=ROOT, capture_output=True, text=True, timeout=180,
                              env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert finished.returncode == 0, finished.stderr[-1500:]
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answer["fd_limit"] is not None and answer["fd_limit"][0] == 64
    assert answer["soft_after"] > 64, answer
    assert answer["upstream_holds"] == 40, answer
    assert answer["probe"] == "served", answer


def test_the_default_target_is_a_number_the_kernel_can_honour() -> None:
    assert 1024 <= DEFAULT_TARGET <= 1 << 20, (
        "the target must be far above the 2-descriptors-per-request budget of §15.1's 200 streams and small enough that "
        "`setrlimit` against a finite hard limit has something to clamp")


@posix_only
def test_the_descriptor_budget_is_read_from_this_process() -> None:
    """The caps are derived from a number read at start-up, so that number has to be the truth, not a constant.

    Read in a subprocess with a deliberately lower soft limit: the point is that `fd_budget()` follows the process, not
    that this machine happens to allow 65536.
    """
    probe = textwrap.dedent("""
        import json, resource, sys
        sys.path.insert(0, "src")
        from blackbox.proxy.limits import fd_budget
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (200, hard))
        print(json.dumps({"budget": fd_budget(), "soft": resource.getrlimit(resource.RLIMIT_NOFILE)[0]}))
    """)
    finished = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert finished.returncode == 0, finished.stderr[-800:]
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answer["soft"] == 200 and answer["budget"] == 200, answer


def test_a_platform_without_a_descriptor_budget_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows publishes no such number; `None` is the answer, and `plan_caps` turns it into policy rather than a claim."""
    monkeypatch.setitem(sys.modules, "resource", None)
    assert fd_budget() is None


@posix_only
def test_a_host_that_allows_200_descriptors_gets_caps_that_fit_200() -> None:
    """The cross-platform half of B3: a `setrlimit` that is refused must shrink the caps, not break the proxy.

    A macOS shell starts at 256 and a container can have any hard limit at all; both are covered by the same arithmetic.
    """
    probe = textwrap.dedent("""
        import json, resource, sys
        sys.path.insert(0, "src")
        from blackbox.proxy.limits import fd_budget
        from blackbox.proxy.server import PROXY_LOCAL_SLACK_FDS, plan_caps
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (200, hard))     # as if the raise had been refused
        caps = plan_caps(fd_budget())
        print(json.dumps({"budget": caps.budget, "connections": caps.max_connections,
                          "inflight": caps.max_inflight, "slack": PROXY_LOCAL_SLACK_FDS,
                          "reason": caps.reason}))
    """)
    finished = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert finished.returncode == 0, finished.stderr[-800:]
    answer = json.loads(finished.stdout.strip().splitlines()[-1])
    assert answer["budget"] == 200, answer
    assert answer["connections"] + answer["inflight"] + answer["slack"] <= 200, answer
    assert answer["connections"] < 512 and answer["inflight"] < 200, answer
    assert "200 descriptors" in answer["reason"], answer
