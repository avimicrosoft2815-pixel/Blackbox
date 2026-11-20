"""Synthetic order-desk client — the *pre-fix* version, kept as the repair proposal's target.

This file is invented for the demo. The bug it contains is the one the recorded incident shows: a stream that ends
without `[DONE]` is treated as a finished message, so the assistant's tool call is acted on as if it had a result.
"""
from __future__ import annotations


class IncompleteMessage(Exception):
    """The assistant message ended before the provider said it was finished."""


def read_stream(lines: list[str]) -> tuple[str, dict]:
    """Fold SSE lines into (text, tool_call). Returns whatever arrived, even if the message is unfinished."""
    text = ""
    tool_call: dict = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            return text, tool_call
        text += body
    return text, tool_call          # BUG: no [DONE] was seen, and this still looks like a complete message


def next_action(tool_call: dict) -> str:
    """What the app does next. With an unfinished message this is a decision made on partial evidence."""
    if tool_call.get("name") == "lookup_order":
        return f"order-lookup:{tool_call.get('arguments', '{}')}"
    return "reply"
