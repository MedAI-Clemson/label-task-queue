"""What a problem in a config is, and the shared wording of the messages about it.

Every message in this package is written for the researcher who wrote the config, not for
a programmer: it says where to look, what was found, and what to write instead. The helpers
here keep that wording the same wherever it is used.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a config (an error), or worth knowing about (a warning).

    ``where`` says where to look, in the researcher's terms: ``"top level"``,
    ``"stage 'expert_review', key 'sample'"`` or ``"line 7"``. ``message`` says what is
    wrong and what to do about it.
    """

    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


# ------------------------------------------------------------------------------ locations


def top_level(key: str | None = None) -> str:
    """The location of the whole document, or of one of its top-level keys."""
    return "top level" if key is None else f"top level, key '{key}'"


def stage_where(name: str | None, position: int, key: str | None = None) -> str:
    """The location of a stage (and optionally one of its keys).

    A stage is called by its name. When it has no usable name, it is called by its
    place in the list, counting from 1.
    """
    label = f"stage '{name}'" if name else f"stage {position}"
    return label if key is None else f"{label}, key '{key}'"


# -------------------------------------------------------------------------------- wording


def closest(word: str, options: Iterable[str]) -> str | None:
    """The option most like ``word`` (ignoring case), or None if nothing is close."""
    by_lower_case = {option.lower(): option for option in options}
    matches = difflib.get_close_matches(word.lower(), list(by_lower_case), n=1, cutoff=0.6)
    return by_lower_case[matches[0]] if matches else None


def suggestion(word: str, options: Iterable[str]) -> str:
    """``"Did you mean 'x'?"`` when an option is close to ``word``, otherwise an empty string."""
    match = closest(word, options)
    return f"Did you mean '{match}'?" if match else ""


def and_list(items: Iterable[object]) -> str:
    """Items in a sentence: ``a``, ``a and b``, ``a, b and c``."""
    words = [str(item) for item in items]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def quoted_list(names: Iterable[str]) -> str:
    """Names in quotes, for a sentence: ``'a'``, ``'a' and 'b'``, ``'a', 'b' and 'c'``."""
    return and_list(f"'{name}'" for name in names)


def sentences(*parts: str) -> str:
    """Join sentences with single spaces, leaving out empty ones."""
    return " ".join(part for part in parts if part)


def plural(count: int, noun: str) -> str:
    """``"1 error"``, ``"2 errors"``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def percent(fraction: float) -> str:
    """A fraction as a percentage: 0.2 becomes ``"20%"``."""
    return f"{fraction * 100:g}%"


def describe(value: object) -> str:
    """Describe a value found in the file, for a message: ``'abc'``, ``1.5``, ``a list``."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{value if len(value) <= 40 else value[:37] + '...'}'"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | tuple):
        return "a list"
    if isinstance(value, Mapping):
        return "a group of keys"
    return f"a value of the wrong kind ({type(value).__name__})"
