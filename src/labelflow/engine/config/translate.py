"""Turn Pydantic's validation errors into plain-language problems.

Pydantic's own messages are written for programmers. Here every problem is described in
the researcher's terms: which stage, which key, what was found, and what to write instead.

Pydantic is only asked *where* something is wrong (which key of which stage). What to say
about it is worked out here from the value in the file, one explainer per key.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from labelflow.engine.config.models import NAME_PATTERN, LabelsFraction, Stage, WorkflowConfig
from labelflow.engine.config.problems import (
    Problem,
    closest,
    describe,
    sentences,
    stage_where,
    suggestion,
    top_level,
)

TOP_LEVEL_KEYS = tuple(WorkflowConfig.model_fields)
STAGE_KEYS = tuple(Stage.model_fields)
FRACTION_KEYS = tuple(LabelsFraction.model_fields)
OUTCOMES = ("agreed", "disagreed")

LABELS_PER_ITEM_FORMS = (
    "It can be a whole number of 1 or more, such as 2 (each item gets that many labels); "
    "`all` (every person holding the stage's roles labels each item); or "
    "`{count: 3, fraction: 0.2}` (20% of items get 3 labels, the rest get 1)."
)

INPUT_HELP = (
    "An input is the name of an earlier stage, optionally followed by .agreed or "
    ".disagreed, for example 'annotate' or 'annotate.disagreed'. To take every item of "
    "the dataset, leave `input` out."
)

MISSING_KEY_HINTS = {
    "workflow": "Give the workflow a name, for example `workflow: xray_main`.",
    "dataset": "Name the dataset whose items enter the workflow, for example "
    "`dataset: xray_batch_1`.",
    "label_task": "Name the label task and its version, for example "
    "`label_task: image_ontology@1`.",
    "stages": "List at least one stage.",
    "name": "Give each stage a name, for example `name: annotate`.",
    "assign_to": "Name the role (or list of roles) whose holders label this stage, for "
    "example `assign_to: annotator`.",
}

# Keys that hold a name, with what to call it and an example.
NAME_KEYS = {
    "workflow": ("workflow name", "xray_main"),
    "dataset": ("dataset name", "xray_batch_1"),
    "name": ("stage name", "annotate"),
    "score_against": ("property name", "gold_key"),
}


def is_name(value: object) -> bool:
    """True when the value is a valid name (stage, role, dataset...)."""
    return isinstance(value, str) and re.fullmatch(NAME_PATTERN, value) is not None


def as_number(value: object) -> float | None:
    """The value as a number if it is one (or text such as '0.5'); booleans are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- explainers, one per key


def _bad_name(value: object, subject: str, noun: str, example: str) -> str:
    if not isinstance(value, str):
        return (
            f"{subject} must be a {noun} written as text, for example {example}; "
            f"got {describe(value)}."
        )
    fixed = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_")
    hint = f" Try '{fixed}'." if fixed != value and is_name(fixed) else ""
    return (
        f"{describe(value)} is not a valid {noun}. "
        f"A name starts with a letter and uses only letters, digits and underscores.{hint}"
    )


def _explain_label_task(value: object) -> list[str]:
    example = "image_ontology@1"
    if not isinstance(value, str):
        return [
            f"`label_task` must be written as name@version, for example {example}; "
            f"got {describe(value)}."
        ]
    name, at, version = value.partition("@")
    if not at:
        suggested = f"{value}@1" if is_name(value) else example
        return [
            f"{describe(value)} has no version. Write the label task as name@version, "
            f"with a whole-number version, for example {suggested}."
        ]
    if not is_name(name):
        return [
            f"{describe(value)} does not start with a valid label task name. Write it as "
            f"name@version, for example {example}. A name starts with a letter and uses "
            "only letters, digits and underscores."
        ]
    return [
        f"{describe(value)} has an invalid version. After the @ there must be a whole "
        f"number such as 1; got {describe(version)}."
    ]


def _explain_input(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [
            sentences(
                "`input` must be a single stage name: a stage takes its items from one "
                "place, not from a list.",
                INPUT_HELP,
            )
        ]
    if not isinstance(value, str):
        return [sentences(f"`input` must be written as text; got {describe(value)}.", INPUT_HELP)]
    stage, dot, outcome = value.partition(".")
    if dot and is_name(stage) and outcome not in OUTCOMES:
        match = closest(outcome, OUTCOMES)
        hint = (
            f"Did you mean '{stage}.{match}'?"
            if match
            else "After the stage name, write .agreed or .disagreed."
        )
        return [f"{describe(value)} is not a valid input: '{outcome}' is not an output. {hint}"]
    return [sentences(f"{describe(value)} is not a valid input.", INPUT_HELP)]


def _explain_assign_to(value: object) -> list[str]:
    if isinstance(value, str):
        return [_bad_name(value, "`assign_to`", "role name", "annotator")]
    if isinstance(value, list | tuple):
        if not value:
            return [
                "`assign_to` is an empty list. Name at least one role whose holders label "
                "this stage."
            ]
        return [
            _bad_name(entry, "Each entry of `assign_to`", "role name", "annotator")
            for entry in value
            if not is_name(entry)
        ]
    return [
        "`assign_to` must be a role name, such as `assign_to: expert`, or a list of role "
        f"names, such as `assign_to: [annotator, expert]`; got {describe(value)}."
    ]


def _explain_final(value: object) -> list[str]:
    form = (
        "`final` must be a list of stage names, such as [expert_review, annotate], "
        "or the word none."
    )
    if isinstance(value, list | tuple):
        return [
            _bad_name(entry, "Each entry of `final`", "stage name", "annotate")
            for entry in value
            if not is_name(entry)
        ]
    hint = ""
    if isinstance(value, str):
        one_stage = f"To use one stage, write `final: [{value}]`." if is_name(value) else ""
        hint = suggestion(value, ["none"]) or one_stage
    return [sentences(form, f"Got {describe(value)}.", hint)]


def _explain_stages(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        # Each broken stage is reported on its own; only an empty list is reported here.
        return [] if value else ["`stages` is empty. A workflow needs at least one stage."]
    return [
        "`stages` must be a list of stages, each starting with '- name: ...'; "
        f"got {describe(value)}."
    ]


def _explain_sample(value: object) -> list[str]:
    number = as_number(value)
    if number is None:
        hint = ""
        if isinstance(value, str) and value.strip().endswith("%"):
            percentage = as_number(value.strip()[:-1])
            if percentage is not None:
                hint = f"Write {value.strip()} as {percentage / 100:g}."
        return [
            sentences(
                "sample must be a number between 0 and 1 (for example 0.2 for 20% of items); "
                f"got {describe(value)}.",
                hint,
            )
        ]
    hint = ""
    if number > 1:
        hint = "sample is a fraction, not a percentage: write 0.2 for 20%."
    elif number == 0:
        hint = "A stage that admits no items does nothing; remove the stage instead."
    return [sentences(f"sample must be more than 0 and at most 1; got {describe(value)}.", hint)]


def _explain_labels_per_item(value: object) -> list[str]:
    if isinstance(value, Mapping):
        return _explain_labels_fraction(value)
    hint = suggestion(value, ["all"]) if isinstance(value, str) else ""
    return [sentences(f"labels_per_item cannot be {describe(value)}.", hint, LABELS_PER_ITEM_FORMS)]


def _explain_labels_fraction(value: Mapping) -> list[str]:
    """Explain what is wrong inside ``labels_per_item: {count: ..., fraction: ...}``."""
    unknown = [key for key in value if key not in FRACTION_KEYS]
    messages = [
        sentences(
            f"labels_per_item has an unknown key '{key}'.",
            suggestion(str(key), FRACTION_KEYS) or "It takes `count` and `fraction`.",
        )
        for key in unknown
    ]
    # A key that was probably misspelled is not also reported as missing.
    typo_targets = {closest(str(key), FRACTION_KEYS) for key in unknown}

    count = value.get("count")
    count_ok = type(count) is int and count >= 2
    if "count" not in value:
        if "count" not in typo_targets:
            messages.append(
                "labels_per_item is missing `count`, the number of labels (2 or more) "
                "for the items chosen by `fraction`."
            )
    elif not count_ok:
        messages.append(
            f"labels_per_item: `count` must be a whole number of 2 or more; got {describe(count)}. "
            "For one label per item, write `labels_per_item: 1`."
        )

    fraction = value.get("fraction")
    number = as_number(fraction)
    every_item = f"To give every item {count} labels, write `labels_per_item: {count}`."
    if "fraction" not in value:
        if "fraction" not in typo_targets:
            messages.append(
                sentences(
                    "labels_per_item is missing `fraction`, the share of items that get "
                    "`count` labels, for example 0.2.",
                    every_item if count_ok else "",
                )
            )
    elif number is None or not 0 < number < 1:
        messages.append(
            sentences(
                "labels_per_item: `fraction` must be more than 0 and less than 1 "
                f"(for example 0.2 for 20%); got {describe(fraction)}.",
                every_item if number == 1 and count_ok else "",
            )
        )
    return messages


def _explain_show_prior(value: object) -> list[str]:
    return [f"show_prior must be true or false; got {describe(value)}."]


EXPLAINERS: dict[str, Callable[[Any], list[str]]] = {
    "label_task": _explain_label_task,
    "stages": _explain_stages,
    "final": _explain_final,
    "input": _explain_input,
    "assign_to": _explain_assign_to,
    "sample": _explain_sample,
    "labels_per_item": _explain_labels_per_item,
    "show_prior": _explain_show_prior,
}


def explain_key(key: str, value: object) -> list[str]:
    """Plain-language messages about a known key that has an unusable value."""
    if key in NAME_KEYS:
        noun, example = NAME_KEYS[key]
        return [_bad_name(value, f"`{key}`", noun, example)]
    return EXPLAINERS[key](value) or [
        f"{key} has a value that cannot be used; got {describe(value)}."
    ]


def unknown_key_message(key: object, allowed: tuple[str, ...], level: str) -> str:
    """Say that a key is not known, suggesting the closest allowed key."""
    return sentences(
        f"'{key}' is not a known key.",
        suggestion(str(key), allowed) or f"The keys allowed {level} are: {', '.join(allowed)}.",
    )


def missing_key_message(key: str) -> str:
    return sentences(f"The required key '{key}' is missing.", MISSING_KEY_HINTS.get(key, ""))


# ------------------------------------------------------------------ the translation itself


@dataclass(frozen=True)
class _Level:
    """One mapping of the document: the top level, or one stage."""

    keys: tuple[str, ...]  # the keys allowed here
    phrase: str  # "at the top level" / "in a stage"
    values: Mapping[str, Any]  # what the file holds here
    position: int | None  # stage number counting from 0; None at the top level

    def where(self, key: str | None = None) -> str:
        if self.position is None:
            return top_level(key)
        name = self.values.get("name")
        return stage_where(name if is_name(name) else None, self.position + 1, key)

    def sort_key(self, key: object) -> tuple[int, int, int]:
        """Where a problem belongs in the document: top-level keys, then stages in order.

        Known keys come in the order the model lists them; unknown keys after them, in the
        order they were written.
        """
        if key in self.keys:
            place = self.keys.index(key)
        else:
            written = list(self.values)
            place = len(self.keys) + (written.index(key) if key in written else 0)
        if self.position is None:
            return (place, -1, 0)
        return (TOP_LEVEL_KEYS.index("stages"), self.position, place)


def _key_path(loc: tuple) -> tuple:
    """Cut an error location down to the key it belongs to.

    ``("stages", 1, "labels_per_item", "LabelsFraction", "count")`` becomes
    ``("stages", 1, "labels_per_item")``, and ``("final", 0)`` becomes ``("final",)``.
    """
    if len(loc) >= 2 and loc[0] == "stages" and isinstance(loc[1], int):
        return loc[:3]
    return loc[:1]


def _level_for(data: Mapping[str, Any], path: tuple) -> _Level | None:
    """The mapping of the document that an error path points into."""
    if path[0] != "stages" or len(path) < 3:
        return _Level(TOP_LEVEL_KEYS, "at the top level", data, None)
    entry = _raw_stage(data, path[1])
    if isinstance(entry, Mapping):
        return _Level(STAGE_KEYS, "in a stage", entry, path[1])
    return None


def _raw_stage(data: Mapping[str, Any], index: int) -> object:
    stages = data.get("stages")
    return stages[index] if isinstance(stages, list | tuple) and index < len(stages) else None


def _kind(path: tuple, errors: list[dict[str, Any]]) -> str:
    """Whether the path is an unknown key, a missing key, or a known key with a bad value."""
    types = {e["type"] for e in errors if len(e["loc"]) == len(path)}
    if types & {"extra_forbidden", "invalid_key"}:
        return "unknown"
    if "missing" in types:
        return "missing"
    return "invalid"


def translate_errors(
    data: Mapping[str, Any], errors: list[dict[str, Any]]
) -> tuple[list[Problem], list[tuple]]:
    """Turn Pydantic's field errors into problems, in the order of the document.

    Returns the problems and the places they were found (paths such as
    ``("stages", 1, "sample")``, stages counting from 0), so that other checks can steer
    clear of values that are already known to be wrong.
    """
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for error in errors:
        groups.setdefault(_key_path(tuple(error["loc"])), []).append(error)
    levels = {path: _level_for(data, path) for path in groups}
    kinds = {path: _kind(path, errs) for path, errs in groups.items()}

    # An unknown key that looks like a missing one is a typo: say it once, on the typo.
    typos = {
        (path[:-1], closest(str(path[-1]), levels[path].keys))
        for path in groups
        if levels[path] is not None and kinds[path] == "unknown"
    }

    found: list[tuple[tuple, Problem]] = []
    places: list[tuple] = []
    for path in groups:
        level, kind, key = levels[path], kinds[path], path[-1]
        if path[0] == "stages" and len(path) == 2:
            # The stage itself is not a group of keys.
            number = key + 1
            message = (
                f"Stage {number} must be a group of keys such as `name` and `assign_to`; "
                f"got {describe(_raw_stage(data, key))}."
            )
            order = (TOP_LEVEL_KEYS.index("stages"), key, -1)
            found.append((order, Problem(f"stage {number}", message)))
            places.append(path)
            continue
        if level is None:
            continue
        value = level.values.get(key)
        if kind == "unknown":
            messages = [unknown_key_message(key, level.keys, level.phrase)]
        elif kind == "missing":
            if (path[:-1], key) in typos:
                continue
            messages = [missing_key_message(key)]
        elif key == "stages" and isinstance(value, list | tuple) and value:
            # Pydantic also complains about the list when a stage inside it is broken.
            # That stage has its own error, so there is nothing more to say here.
            continue
        else:
            messages = explain_key(key, value)
        places.append(path)
        found += [(level.sort_key(key), Problem(level.where(key), m)) for m in messages]

    found.sort(key=lambda item: item[0])
    return [problem for _, problem in found], places
