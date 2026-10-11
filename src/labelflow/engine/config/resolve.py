"""The resolved view of a config: every default filled in, ready to read or to re-parse.

``resolve`` gives a plain dict and ``resolved_yaml`` gives readable YAML with short
comments. Both can be read back with ``WorkflowConfig.model_validate`` (or by parsing the
YAML) and give a config that resolves identically.
"""

from __future__ import annotations

from typing import Any

import yaml

from labelflow.engine.config.models import LabelsFraction, Stage, WorkflowConfig
from labelflow.engine.config.problems import percent


def resolve(config: WorkflowConfig) -> dict[str, Any]:
    """The config with every default filled in, as plain dicts, lists and values.

    Per stage: ``input`` is None when the stage takes every item of the dataset,
    ``assign_to`` is always a list, and ``labels_per_item`` is a whole number, ``"all"`` or
    ``{"count": ..., "fraction": ...}``. ``final`` is a list of stage names, or ``"none"``.
    """
    final = config.resolved_final()
    return {
        "workflow": config.workflow,
        "dataset": config.dataset,
        "label_task": config.label_task,
        "stages": [_resolve_stage(stage) for stage in config.stages],
        "final": "none" if final is None else list(final),
    }


def _resolve_stage(stage: Stage) -> dict[str, Any]:
    labels = stage.labels_per_item
    if isinstance(labels, LabelsFraction):
        labels = {"count": labels.count, "fraction": labels.fraction}
    return {
        "name": stage.name,
        "input": stage.input,
        "assign_to": list(stage.roles),
        "sample": stage.sample,
        "labels_per_item": labels,
        "score_against": stage.score_against,
        "show_prior": stage.show_prior,
    }


# ----------------------------------------------------------------------------- YAML

COMMENT_COLUMN = 34


def resolved_yaml(config: WorkflowConfig) -> str:
    """The resolved config as YAML, with short comments saying what each setting does."""
    resolved = resolve(config)
    lines = [
        f"# The resolved config of workflow '{config.workflow}': every default is filled in.",
        _line("", "workflow", resolved["workflow"]),
        _line("", "dataset", resolved["dataset"]),
        _line("", "label_task", resolved["label_task"]),
        "",
        "stages:",
    ]
    for index, stage in enumerate(resolved["stages"]):
        if index:
            lines.append("")
        lines += _stage_lines(stage, config.dataset)
    lines.append("")
    lines.append(_line("", "final", resolved["final"], _final_comment(resolved["final"])))
    return "\n".join(lines) + "\n"


def _stage_lines(stage: dict[str, Any], dataset: str) -> list[str]:
    roles = stage["assign_to"]
    roles_comment = "anyone holding any of these roles" if len(roles) > 1 else ""
    labels = stage["labels_per_item"]
    return [
        _line("  - ", "name", stage["name"]),
        _line("    ", "input", stage["input"], _input_comment(stage["input"], dataset)),
        _line("    ", "assign_to", roles, roles_comment),
        _line("    ", "sample", stage["sample"], _sample_comment(stage["sample"])),
        _line("    ", "labels_per_item", labels, _labels_comment(labels)),
        _line(
            "    ",
            "score_against",
            stage["score_against"],
            _score_comment(stage["score_against"]),
        ),
        _line("    ", "show_prior", stage["show_prior"], _prior_comment(stage["show_prior"])),
    ]


def _line(indent: str, key: str, value: Any, comment: str = "") -> str:
    """One ``key: value`` line, with an optional comment lined up to the right."""
    text = f"{indent}{key}: {_value(value)}"
    return f"{text.ljust(COMMENT_COLUMN)}  # {comment}" if comment else text


def _value(value: Any) -> str:
    """A value as YAML: scalars quoted only when needed, lists and groups on one line."""
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {_value(item)}" for key, item in value.items()) + "}"
    dumped = yaml.safe_dump(value, default_flow_style=True, width=1000)
    return dumped.removesuffix("\n").removesuffix("\n...")


def _input_comment(value: str | None, dataset: str) -> str:
    if value is None:
        return f"every item of the dataset '{dataset}'"
    stage, _, outcome = value.partition(".")
    if outcome == "agreed":
        return f"items of '{stage}' whose labels agreed"
    if outcome == "disagreed":
        return f"items of '{stage}' whose labels disagreed"
    return f"every item that completed '{stage}'"


def _sample_comment(sample: float) -> str:
    if sample == 1:
        return "every arriving item enters"
    return f"{percent(sample)} of arriving items enter (by hashed draw)"


def _labels_comment(labels: Any) -> str:
    if labels == "all":
        return "everyone holding the roles at admission labels it"
    if isinstance(labels, dict):
        return (
            f"{percent(labels['fraction'])} of items get {labels['count']} labels, the rest get 1"
        )
    if labels == 1:
        return "each item is labeled once"
    return f"each item is labeled by {labels} different people"


def _score_comment(score_against: str | None) -> str:
    if score_against is None:
        return "labels are not scored against a key"
    return f"each label is scored against the item property '{score_against}'"


def _prior_comment(show_prior: bool) -> str:
    if show_prior:
        return "labelers see earlier labels"
    return "blind: labelers do not see earlier labels"


def _final_comment(final: list[str] | str) -> str:
    if final == "none":
        return "this workflow produces no final labels"
    return "the first listed stage that labeled an item gives its label"
