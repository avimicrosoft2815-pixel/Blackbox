"""A stream that ends without its provider's terminator is not a finished stream (defect 66).

Reproduced before this fix with the mock provider truncating 3 of 6 chunks and a real `openai` client: the SDK saw a
clean end of body, the script exited 0, and the capsule recorded `MODEL_RESPONSE` with `partial: False` — so
`blackbox diagnose` said *"nothing in these frames indicates that anything was cut short"* about a call that was cut
short. The app itself could not tell either; that is exactly the failure this recorder exists to make visible.

The rule is now a **three-valued** answer, because two were not enough: `terminated` (the provider's own marker was
seen), `unterminated` (a convention this build knows, and its marker never came — the stream was cut, so `partial`), and
`unverified` (no convention this build knows, so **nothing is claimed**: the capsule says the ending is unknown instead
of printing `partial: False`, which read as "complete"). A generic `text/event-stream` has no terminator in its
specification; calling it complete would be a guess, and calling it cut would be a different guess.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackbox.cap_sdk.base import ModelCall
from blackbox.cap_sdk.tee import TAIL_BYTES, TerminatorWatch, expected_terminators, wrap_stream


class _Recorder:
    """The smallest thing `wrap_stream` accepts: it counts chunks and remembers how the call was closed."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed: dict[str, object] = {}

    def open_call(self, *args: object, **kwargs: object) -> ModelCall:
        raise AssertionError("the tee must not open calls; the seam does that")

    def receive(self, call: ModelCall, chunk: bytes, **kwargs: object) -> None:
        self.chunks.append(chunk)

    def close_call(self, call: ModelCall, **kwargs: object) -> None:
        self.closed = {"partial": call.partial, "end_reason": call.end_reason, **kwargs}


def _sync_stream(chunks: list[bytes]) -> object:
    class _Inner:
        def iter_bytes(self) -> object:
            yield from chunks

        def __iter__(self) -> object:
            yield from chunks

    return _Inner()


def _stream_through(path: str, chunks: list[bytes]) -> tuple[ModelCall, _Recorder]:
    call = ModelCall(provider="127.0.0.1", path=path, stream=True)
    recorder = _Recorder()
    tee = wrap_stream(_sync_stream(chunks), recorder, call, 200, 0)   # type: ignore[arg-type]
    for _ in tee:
        pass
    return call, recorder


def test_an_openai_stream_without_done_is_partial_and_says_why() -> None:
    """The reproduced case: three data events, then the other end hangs up."""
    call, recorder = _stream_through(
        "/v1/chat/completions",
        [b'data: {"choices":[{"delta":{"content":"token "}}]}\n\n',
         b'data: {"choices":[{"delta":{"content":"token "}}]}\n\n',
         b'data: {"choices":[{"delta":{"content":"token "}}]}\n\n'])
    assert call.partial is True, "a stream that never carried [DONE] was recorded as a finished one"
    assert call.ending == "unterminated" and call.ending_provider == "openai"
    assert call.end_reason is not None and "[DONE]" in call.end_reason
    assert recorder.closed["partial"] is True and recorder.closed["end_reason"] == call.end_reason


def test_an_openai_stream_with_done_is_complete() -> None:
    call, recorder = _stream_through(
        "/v1/chat/completions",
        [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"])
    assert call.partial is False
    assert call.ending == "terminated" and call.ending_provider == "openai"
    assert call.end_reason is None
    assert recorder.closed["partial"] is False


def test_a_marker_split_across_two_chunks_is_still_seen() -> None:
    """`data: [DO` + `NE]` is one terminator; testing each chunk alone would call a good stream truncated."""
    call, _ = _stream_through("/v1/chat/completions", [b'data: {"x":1}\n\n', b"data: [DO", b"NE]\n\n"])
    assert call.partial is False, "the terminator straddled two chunks and was missed"


def test_an_anthropic_stream_without_message_stop_is_partial() -> None:
    call, _ = _stream_through("/v1/messages", [b"event: content_block_delta\ndata: {}\n\n"])
    assert call.partial is True
    assert call.end_reason is not None and "message_stop" in call.end_reason


def test_an_anthropic_stream_with_message_stop_is_complete() -> None:
    call, _ = _stream_through("/v1/messages", [b"event: message_stop\ndata: {}\n\n"])
    assert call.partial is False and call.ending == "terminated" and call.ending_provider == "anthropic"


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v1/models", "/some/other/stream", "", "/v1/chat/streaming"])
def test_an_unknown_convention_is_recorded_as_unverified_not_as_complete(path: str) -> None:
    """The owner's rule: never claim a stream is complete when its ending is unknown.

    `partial: False` used to be the whole answer for these paths, and a reader had no way to tell "finished" from
    "nobody checked". Now the capsule says `ending: unverified`, `ending_provider: unrecognised` and explains itself, and
    `partial` stays False because "we did not check" is not the same claim as "it was cut".
    """
    call, _ = _stream_through(path, [b"data: whatever\n\n"])
    assert call.ending == "unverified", f"{path!r} was judged against a convention nobody declared"
    assert call.ending_provider == "unrecognised"
    assert call.partial is False, "unverified is not evidence of a cut"
    assert call.end_reason is not None and "not recorded" in call.end_reason


def test_the_watch_keeps_only_a_tail() -> None:
    """A 200 MB stream must cost the tee 128 bytes of memory, and a marker outside the tail cannot be claimed."""
    watch = TerminatorWatch("/v1/chat/completions")
    for _ in range(1000):
        watch.feed(b"x" * 1024)
    assert len(watch.tail) == TAIL_BYTES
    assert watch.saw_terminator() is False and watch.reason() is not None
    watch.feed(b"data: [DONE]\n\n")
    assert watch.saw_terminator() is True and watch.reason() is None


def test_the_terminator_table_names_what_it_covers() -> None:
    assert expected_terminators("/v1/chat/completions") == [b"[DONE]", b"[DONE]"]
    assert expected_terminators("/v1/messages") == [b"message_stop"]
    assert expected_terminators("/v1/embeddings") == []


@pytest.mark.parametrize(("path", "marker", "provider"), [
    ("/v1/chat/completions", b"data: [DONE]", "openai"),
    ("/v1/completions", b"data: [DONE]", "openai"),
    ("/v1/responses", b"event: response.completed\ndata: {}", "openai"),
    ("/v1/messages", b"event: message_stop", "anthropic"),
    ("/v1beta/models/gemini-2.0:streamGenerateContent", b'{"candidates":[{"finishReason":"STOP"}]}', "google"),
    ("/api/chat", b'{"message":{},"done":true}', "ollama"),
    ("/api/generate", b'{"response":"hi","done":true}', "ollama"),
])
def test_each_supported_provider_is_judged_by_its_own_convention(path: str, marker: bytes, provider: str) -> None:
    """The providers this build can vouch for, one row each: marker seen is complete, marker missing is a cut."""
    complete, _ = _stream_through(path, [b'data: {"x":1}\n\n', marker])
    assert (complete.ending, complete.ending_provider, complete.partial) == ("terminated", provider, False)
    cut, _ = _stream_through(path, [b'data: {"x":1}\n\n'])
    assert (cut.ending, cut.ending_provider, cut.partial) == ("unterminated", provider, True)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/api/chat"])
def test_a_marker_from_another_provider_does_not_count(path: str) -> None:
    """Anthropic's `message_stop` in an OpenAI stream is not that stream finishing: each row is its own convention."""
    call, _ = _stream_through(path, [b"event: message_stop\ndata: {}\n\n"])
    assert call.ending == "unterminated", call.end_reason


def test_a_non_streaming_json_body_is_not_judged() -> None:
    """A 200 with a plain JSON body has no stream convention at all; the watch must not invent one."""
    watch = TerminatorWatch("/v1/chat/completions")
    watch.feed(b'{"id":"cmpl-1","choices":[{"message":{"content":"hi"}}]}')
    assert watch.ending() == "unterminated"
