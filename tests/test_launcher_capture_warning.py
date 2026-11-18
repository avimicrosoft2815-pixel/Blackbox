"""A run that captured nothing has to say so (B2 in `docs/RELEASE-BLOCKERS.md`).

The reproduction this prevents: `OPENAI_BASE_URL` points at a gateway, `blackbox run -- python app.py` adopts it as an
upstream rather than overwriting it, the run prints `recovered … root …`, `blackbox ls` shows a capsule, and that capsule
holds `RUN_START`, two `CHAIN`, `RUN_END`, `SEG_END` — nothing about the call. The manifest and the report are honest now
(`coverage: NONE`); this file is the other half, the line on the user's terminal.

The warning must also **not** fire on a run that did capture, and it must not read as an error for a command that simply
makes no model call (`blackbox run -- echo hi`).
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.rec.launcher import _limit_warning, launch

WARNING = "this run recorded no model call"

#: Talks to the proxy the launcher exports, with no SDK: one request through it is one recorded model call.
_CLIENT = """
import json, os, urllib.request
base = os.environ["BLACKBOX_PROXY_URL"]
try:
    with urllib.request.urlopen(base + "/v1/models", timeout=5) as response:
        print("status", response.status)
except Exception as exc:                       # a 502 is fine here: the point is the request was recorded
    print("error", type(exc).__name__)
"""


def _run(tmp_path: Path, *cmd: str, env: dict[str, str] | None = None,
         proxy: bool = True) -> tuple[object, str]:
    out = io.StringIO()
    result = launch(list(cmd), data_dir=str(tmp_path / "data"), out=out, proxy=proxy,
                    env=env if env is not None else dict(os.environ))
    return result, out.getvalue()


def test_a_run_that_made_no_model_call_says_so(tmp_path: Path) -> None:
    result, text = _run(tmp_path, sys.executable, "-c", "print('hello')")
    assert result.exit_code == 0
    assert WARNING in text, text
    assert result.captured["model_calls"] == 0
    assert "not a failure" in text, "the line must not read as an error for a command that makes no model call"


def test_the_warning_names_the_cause_and_the_flag_that_fixes_it(tmp_path: Path) -> None:
    _result, text = _run(tmp_path, sys.executable, "-c", "print('hello')")
    assert "adopted as an upstream" in text, text
    assert "--force-proxy" in text, text


def test_a_run_that_reached_the_recorder_is_not_warned_about(tmp_path: Path) -> None:
    result, text = _run(tmp_path, sys.executable, "-c", _CLIENT,
                        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.exit_code == 0, text
    assert result.captured["model_calls"] >= 1, result.captured
    assert WARNING not in text, text


def test_no_warning_without_a_proxy(tmp_path: Path) -> None:
    """`--no-proxy` means nothing was ever going to be recorded; a warning there would be noise about a chosen flag."""
    out = io.StringIO()
    result = launch([sys.executable, "-c", "print('hello')"], data_dir=str(tmp_path / "data"), out=out, proxy=False,
                    env=dict(os.environ))
    assert WARNING not in out.getvalue()
    assert result.exit_code == 0


def test_the_run_report_says_what_happened_at_the_ceiling() -> None:
    """B3: a run that hit the proxy's ceiling must not report a clean summary and say nothing (see
    `tests/test_proxy_backpressure.py` for the behaviour itself; this is the sentence the user reads)."""
    import types

    refused = types.SimpleNamespace(proxy={"stats": {
        "refused": 2, "refused_reasons": {"connection-limit": 2}, "queued": 3, "max_queue_wait_ms": 812.5}})
    text = _limit_warning(refused)
    assert text is not None
    assert "answered 2 requests itself instead of passing them through" in text
    assert "connection-limit: 2" in text
    assert "3 requests waited for a slot at the 200-request ceiling, the longest 812 ms" in text
    assert "never reached a provider" in text

    queued_only = types.SimpleNamespace(proxy={"stats": {"refused": 0, "queued": 1, "max_queue_wait_ms": 4.0}})
    single = _limit_warning(queued_only)
    assert single is not None and "1 request waited for a slot" in single

    quiet = types.SimpleNamespace(proxy={"stats": {"refused": 0, "queued": 0, "max_queue_wait_ms": 0.0}})
    assert _limit_warning(quiet) is None
    assert _limit_warning(types.SimpleNamespace(proxy={})) is None
    assert _limit_warning(types.SimpleNamespace(proxy={})) is None
