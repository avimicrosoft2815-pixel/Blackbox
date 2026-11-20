"""The check this repository offers: a truncated stream must not look like a finished message."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import IncompleteMessage, read_stream  # noqa: E402


def test_a_stream_that_ends_without_done_is_refused() -> None:
    lines = ['data: {"delta": "hello"}', 'data: {"delta": " wor"}']
    try:
        read_stream(lines)
    except IncompleteMessage:
        return
    raise AssertionError("a stream with no [DONE] was accepted as a complete message")


def test_a_finished_stream_is_still_accepted() -> None:
    text, _call = read_stream(['data: {"delta": "hello"}', "data: [DONE]"])
    assert text == '{"delta": "hello"}'
