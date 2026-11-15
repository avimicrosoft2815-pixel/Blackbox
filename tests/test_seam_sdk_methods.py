"""P3-05c — seam C, wrapping the SDK's own methods. Tested against the **real** `openai` and `anthropic` packages.

That choice is the whole point of the file. A fixture that imitates an SDK can only prove the seam agrees with my
idea of the SDK; the day-14 rule is about scripts nobody edited, calling methods nobody stubbed. So these tests
import the published libraries, point them at the loopback mock provider with a fake key, and check what the recorder
saw. They skip — loudly, with the reason — when the SDKs are not installed, and `SUPPORTED.md` records which versions
were verified here.

The claims are narrower than the other seams on purpose: this one counts events, not bytes, so every call it records
is `partial=True` and `bytes_received` is 0. The tests assert that rather than papering over it.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from blackbox.cap_sdk.base import Recorder, SeamUnavailable
from blackbox.cap_sdk.mock_provider import MockProvider, Scenario
from blackbox.cap_sdk.seam_sdk_methods import NAME, install, probe

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")

MESSAGES = [{"role": "user", "content": "name three colours"}]


def _openai_client(provider: MockProvider):
    return openai.OpenAI(api_key="test-key", base_url=provider.openai_base_url(), max_retries=0)


def _anthropic_client(provider: MockProvider):
    return anthropic.Anthropic(api_key="test-key", base_url=provider.anthropic_base_url(), max_retries=0)


def test_probe_names_the_versions_it_found():
    ok, reason = probe()
    assert ok, reason
    assert "openai" in reason and "anthropic" in reason


def test_a_non_streaming_openai_call_is_recorded_with_its_model():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=3)) as provider:
            completion = _openai_client(provider).chat.completions.create(
                model="mock-1", messages=MESSAGES, stream=False)
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.provider == "openai" and call.path == "/v1/chat/completions"
    assert call.model == "mock-1"
    assert call.chunks == 1
    assert call.stream is False
    assert call.status == 200 and call.error is None
    assert "token " in completion.choices[0].message.content
    assert call.request_fp, "the request fingerprint is what makes two identical calls recognisable"


def test_a_streaming_openai_call_counts_the_events_the_application_saw():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=5)) as provider:
            stream = _openai_client(provider).chat.completions.create(
                model="mock-1", messages=MESSAGES, stream=True)
            pieces = [chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices]
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert len(pieces) >= 5
    assert call.chunks == len(pieces), "the seam counted a different number of events than the caller consumed"
    assert call.stream is True
    assert call.partial is True and call.bytes_received == 0, "seam C must not claim wire bytes it never saw"


def test_a_streaming_anthropic_call_counts_its_events():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=4)) as provider:
            stream = _anthropic_client(provider).messages.create(
                model="mock-1", max_tokens=16, messages=MESSAGES, stream=True)
            events = list(stream)
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.provider == "anthropic" and call.path == "/v1/messages"
    assert call.chunks == len(events) >= 4
    assert call.stream is True


def test_embeddings_are_recorded_under_their_own_path():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider() as provider:
            _openai_client(provider).embeddings.create(model="mock-1", input="hello")
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.path == "/v1/embeddings"
    assert call.model == "mock-1"
    assert call.chunks == 1


def test_an_async_streaming_call_is_counted_too():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(chunks=4)) as provider:
            async def run() -> int:
                client = openai.AsyncOpenAI(api_key="test-key", base_url=provider.openai_base_url(),
                                            max_retries=0)
                stream = await client.chat.completions.create(model="mock-1", messages=MESSAGES, stream=True)
                seen = 0
                async for _event in stream:
                    seen += 1
                await client.close()
                return seen

            seen = asyncio.run(run())
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert seen >= 4
    assert call.chunks == seen


def test_the_sdk_signature_survives_the_patch():
    """P3-05c's Done-when asks for this by name: a wrapped method must still look like the method."""
    from anthropic.resources.messages import Messages
    from openai.resources.chat.completions import Completions

    before = inspect.signature(Completions.create)
    handle = install(Recorder())
    try:
        assert inspect.signature(Completions.create) == before
        assert inspect.signature(Messages.create).parameters.get("model") is not None
    finally:
        handle.uninstall()
    assert inspect.signature(Completions.create) == before


def test_an_sdk_shape_this_seam_does_not_recognise_is_skipped_not_guessed():
    class Unknown:
        pass

    recorder = Recorder()
    with pytest.raises(SeamUnavailable) as excinfo:
        install(recorder, targets=(("blackbox_not_a_module", "Nope", "create", "x", "/x"),))
    assert "no known SDK method was found to patch" in str(excinfo.value)

    handle = install(recorder, targets=(("openai.resources.chat.completions", "Completions", "create",
                                        "openai", "/v1/chat/completions"),
                                       ("openai.resources.chat.completions", "NotAClass", "create",
                                        "openai", "/x")))
    try:
        assert len(handle.detail["patched"]) == 1
        assert any("NotAClass" in entry for entry in handle.detail["skipped"])
        assert Unknown is not None
    finally:
        handle.uninstall()


def test_uninstall_puts_every_method_back():
    from anthropic.resources.messages import Messages
    from openai.resources.chat.completions import Completions

    original_chat = Completions.create
    original_messages = Messages.create
    handle = install(Recorder())
    try:
        assert Completions.create is not original_chat
        assert Messages.create is not original_messages
    finally:
        handle.uninstall()
    assert Completions.create is original_chat
    assert Messages.create is original_messages
    assert handle.name == NAME


def test_a_call_that_raises_is_recorded_as_an_error_and_re_raised():
    recorder = Recorder()
    handle = install(recorder)
    try:
        with MockProvider(Scenario(status=429)) as provider, \
                pytest.raises(openai.APIStatusError):
            _openai_client(provider).chat.completions.create(model="mock-1", messages=MESSAGES)
    finally:
        handle.uninstall()

    call = recorder.calls[0]
    assert call.error is not None and "429" in call.error
    assert call.status == 0, "a call that never completed has no status of its own"
