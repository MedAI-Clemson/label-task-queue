"""Small helpers for plain-language messages, shared by the label task modules."""

from __future__ import annotations

import difflib
from collections.abc import Sequence


def describe_kind(value: object) -> str:
    """Name the kind of a value for a message: "null", "a number", "text", "a list"."""
    if value is None:
        return "null"
    kinds = {
        bool: "true or false",
        int: "a number",
        float: "a number",
        str: "text",
        list: "a list",
        tuple: "a list",
        dict: "an object",
    }
    return kinds.get(type(value), f"a {type(value).__name__}")


def quoted(items: Sequence[object]) -> str:
    """Show items in quotes, separated by commas: 'PA', 'AP', 'lateral'."""
    return ", ".join(repr(item) for item in items)


def did_you_mean(word: object, options: Sequence[str]) -> str:
    """A leading-space suggestion such as " Did you mean 'view'?", or "" if nothing is close."""
    if not isinstance(word, str):
        return ""
    if close := difflib.get_close_matches(word, list(options), n=1):
        return f" Did you mean {close[0]!r}?"
    return ""
