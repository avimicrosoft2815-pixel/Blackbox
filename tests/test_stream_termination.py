"""A stream that ends without its provider's terminator is not a finished stream (defect 66).

Reproduced before this fix with the mock provider truncating 3 of 6 chunks and a real `openai` client: the SDK saw a
clean end of body, the script exited 0, and the capsule recorded `MODEL_RESPONSE` with `partial: False` — so
`blackbox diagnose` said *"nothing in these frames indicates that anything was cut short"* about a call that was cut
short. The app itself could not tell either; that is exactly the failure this recorder exists to make visible.

The rule is **conservative on purpose**: only paths whose convention is known are judged (OpenAI's `[DONE]`, Anthropic's
`message_stop`). A generic `text/event-stream` has no terminator in its specification, so an unknown path is never
marked — recording a fact, not guessing at somebody else's protocol.
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
    assert call.end_reason is not None and "[DONE]" in call.end_reason
    assert recorder.closed["partial"] is True and recorder.closed["end_reason"] == call.end_reason


def test_an_openai_stream_with_done_is_complete() -> None:
    call, recorder = _stream_through(
        "/v1/chat/completions",
        [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"])
    assert call.partial is False
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
    assert call.partial is False


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v1/models", "/some/other/stream", ""])
def test_an_unknown_convention_is_never_marked(path: str) -> None:
    """A generic SSE stream has no terminator in its specification: marking it incomplete would be a guess."""
    call, _ = _stream_through(path, [b"data: whatever\n\n"])
    assert call.partial is False, f"{path!r} was judged against a convention nobody declared"
    assert call.end_reason is None


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
