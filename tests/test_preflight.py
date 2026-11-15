"""P3-05d — the ordered preflight. Its Done-when is one sentence: *each seam has a forced-failure test proving the
next is chosen and recorded*.

So each test here forces a specific seam to fail and then checks two things at once — that the next seam is the one
that installed, and that the record says exactly why the earlier one did not. The second half is the part that
matters in three months: a capsule that says `capture_seam: httpx-transport` is only useful if the reader can also
see that seam A was unavailable rather than unlucky.
"""
from __future__ import annotations

import pytest

from blackbox.cap_sdk import preflight
from blackbox.cap_sdk.base import Recorder
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario

httpx = pytest.importorskip("httpx")


def _payload() -> dict:
    return {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def test_the_default_order_is_the_plans_a_then_b():
    names = [name for name, _ in preflight.default_seams()]
    assert names == ["httpx-hooks", "httpx-transport"]


def test_the_first_working_seam_installs_and_is_recorded():
    recorder = Recorder()
    result = preflight.choose(recorder)
    try:
        assert result.capturing
        assert result.seam == "httpx-hooks"
        assert recorder.installed_seam == "httpx-hooks"
        assert [a.status for a in result.attempts] == ["installed"]
        assert result.as_dict()["capture_seam"] == "httpx-hooks"
    finally:
        assert result.handle is not None
        result.handle.uninstall()


def test_forcing_a_to_fail_chooses_b_and_records_the_reason():
    recorder = Recorder()
    reason = "hooks unavailable: client already built its own transport"
    result = preflight.choose(recorder, forced={"httpx-hooks": reason})
    try:
        assert result.seam == "httpx-transport"
        assert recorder.installed_seam == "httpx-transport"
        statuses = [(a.name, a.status) for a in result.attempts]
        assert statuses == [("httpx-hooks", "forced-failed"), ("httpx-transport", "installed")]
        assert result.attempts[0].reason == reason
    finally:
        assert result.handle is not None
        result.handle.uninstall()


def test_forcing_both_leaves_the_recorder_installed_but_not_capturing():
    result = preflight.choose(Recorder(), forced={"httpx-hooks": "forced", "httpx-transport": "forced"})
    assert result.seam is None
    assert result.capturing is False
    assert result.handle is None
    assert [a.status for a in result.attempts] == ["forced-failed", "forced-failed"]
    assert result.as_dict()["capture_seam"] is None


def test_an_absent_library_records_unavailable_not_failed():
    """The difference is the point: `unavailable` means the machine cannot support this seam, `forced-failed` means
    the attempt happened and went wrong. A reader debugging a capture needs to know which."""
    class NoHttpx:
        @staticmethod
        def probe() -> tuple[bool, str]:
            return False, "httpx is not importable: ModuleNotFoundError"

        @staticmethod
        def install(recorder: object) -> None:      # pragma: no cover - must not be called
            raise AssertionError("install was called for an unavailable seam")

    result = preflight.choose(Recorder(), seams=[("httpx-hooks", NoHttpx)])
    assert result.seam is None
    assert result.attempts[0].status == "unavailable"
    assert "ModuleNotFoundError" in result.attempts[0].reason


def test_an_install_that_raises_does_not_escape_the_preflight():
    class Exploding:
        @staticmethod
        def probe() -> tuple[bool, str]:
            return True, "looks fine"

        @staticmethod
        def install(recorder: object) -> None:
            raise RuntimeError("patch refused")

    result = preflight.choose(Recorder(), seams=[("httpx-hooks", Exploding)])
    assert result.seam is None
    assert result.attempts[0].status == "forced-failed"
    assert "patch refused" in result.attempts[0].reason


def test_a_client_passed_to_the_preflight_is_wrapped_rather_than_a_class_patched():
    with MockProvider(Scenario(chunks=3)) as provider:
        recorder = Recorder()
        client = httpx.Client(base_url=provider.base_url, timeout=10.0)
        result = preflight.choose(recorder, client=client)
        try:
            assert result.seam is not None and result.handle is not None
            client.post("/v1/chat/completions", json=_payload())
            assert len(recorder.calls) == 1, "the seam was installed but captured nothing"
        finally:
            result.handle.uninstall()
            client.close()


def test_the_decision_survives_being_written_to_a_manifest():
    result = preflight.choose(Recorder(), forced={"httpx-hooks": "simulated"})
    try:
        payload = result.as_dict()
        assert payload["capture_seam"] == "httpx-transport"
        assert [row["seam"] for row in payload["attempts"]] == ["httpx-hooks", "httpx-transport"]
        assert all(set(row) == {"seam", "status", "reason"} for row in payload["attempts"])
    finally:
        assert result.handle is not None
        result.handle.uninstall()
