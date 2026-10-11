"""Check a labeling workflow config and report every problem in plain language.

``validate_config`` never raises for bad input. It returns a ``ConfigReport`` with the
parsed config (when there are no errors) and every error and warning found, rather than
stopping at the first.

Optionally the config is also checked against what the project already has (its roles,
datasets and label tasks), with ``ValidationContext``.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from ltq.engine.config.graph import ConfigGraphError, check_graph
from ltq.engine.config.models import Stage, WorkflowConfig
from ltq.engine.config.problems import (
    Problem,
    percent,
    plural,
    quoted_list,
    sentences,
    stage_where,
    suggestion,
    top_level,
)
from ltq.engine.config.translate import describe, translate_errors

# --------------------------------------------------------------------------- the report


@dataclass(frozen=True)
class ConfigReport:
    """The result of checking a config.

    ``config`` is the parsed config, or None when there are errors. A config with only
    warnings is still ok.
    """

    config: WorkflowConfig | None
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        """The report as plain text, for the terminal or an admin page."""
        if self.errors:
            fix = "it" if len(self.errors) == 1 else "them"
            summary = (
                f"The config has {plural(len(self.errors), 'error')}. Fix {fix} and check again."
            )
        else:
            summary = "The config is valid."
            if self.warnings:
                summary += f" It has {plural(len(self.warnings), 'warning')}."
        lines = [summary]
        lines += _numbered("Errors", self.errors)
        lines += _numbered("Warnings", self.warnings)
        return "\n".join(lines) + "\n"


def _numbered(title: str, problems: list[Problem]) -> list[str]:
    if not problems:
        return []
    indent = " " * (len(str(len(problems))) + 4)
    lines = ["", f"{title}:"]
    for number, problem in enumerate(problems, start=1):
        lines.append(f"  {number}. {problem.where}")
        lines.append(_wrap(problem.message, indent))
    return lines


def _wrap(message: str, indent: str) -> str:
    """Wrap a message to fit a terminal, without breaking a `code example` across lines."""
    protected = re.sub(r"`[^`]*`", lambda code: code.group().replace(" ", "\x00"), message)
    wrapped = textwrap.fill(
        protected,
        width=92,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped.replace("\x00", " ")


@dataclass(frozen=True)
class ValidationContext:
    """What the project already has. Anything left as None is not checked.

    ``roles`` maps each role defined in the project to the ids of the people who hold it
    now. ``datasets`` lists the dataset names and ``label_tasks`` the available label
    tasks, written as ``name@version``.
    """

    roles: Mapping[str, Collection[str]] | None = None
    datasets: Collection[str] | None = None
    label_tasks: Collection[str] | None = None


# ------------------------------------------------------------------- reading the source


class _Loader(yaml.SafeLoader):
    """A YAML loader that also remembers keys written twice in the same group.

    Plain YAML silently keeps the last one, which could hide a mistake.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.repeated_keys: list[tuple[object, int]] = []

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        seen = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag.endswith(":merge"):
                continue
            key = self.construct_object(key_node)
            if key in seen:
                self.repeated_keys.append((key, key_node.start_mark.line + 1))
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# What PyYAML says, in plain words. The first entry whose text appears in the message wins.
_YAML_EXPLANATIONS = (
    (
        "found character '\\t'",
        "A tab character is used where YAML needs spaces. Indent with spaces.",
    ),
    (
        "mapping values are not allowed here",
        "There is a ':' where YAML does not expect one. Check that this line is indented "
        "like its neighbours, and put any text that contains ': ' in quotes.",
    ),
    (
        "could not find expected ':'",
        "A line in a group of keys is not written as `key: value`. Check for a missing ':' "
        "after the key.",
    ),
    (
        "expected <block end>",
        "A line is indented differently from the lines around it. Lines in the same group "
        "must line up exactly.",
    ),
    (
        "did not find expected '-' indicator",
        "Items of a list must each start with '- ' and line up exactly.",
    ),
    ("expected ',' or ']'", "A list in square brackets is missing a ',' or its closing ']'."),
    ("expected ',' or '}'", "A group in curly braces is missing a ',' or its closing '}'."),
    ("found unexpected end of stream", "A quoted text is missing its closing quote."),
    (
        "but found another document",
        "The file holds more than one YAML document (a line with only '---' starts a new "
        "one). A workflow is a single document: remove the extra '---' line.",
    ),
)


# For these, YAML notices the problem only after the line that is wrong, so point at the
# line where the unfinished construct (a key, a quote, a bracket) started.
_REPORT_WHERE_IT_STARTED = (
    "could not find expected ':'",
    "unexpected end of stream",
    "expected ',' or",
)


def _yaml_problem(error: Exception, text: str) -> Problem:
    """Describe a failure to read the text as YAML, in plain words."""
    if isinstance(error, yaml.reader.ReaderError):
        line = text.count("\n", 0, error.position) + 1
        code = error.character if isinstance(error.character, int) else ord(error.character)
        return Problem(
            f"line {line}",
            f"The file contains a character that YAML does not allow (a control character, "
            f"code {code:#06x}). Delete it, or retype the line.",
        )
    if isinstance(error, RecursionError):
        return Problem(top_level(), "The file is nested too deeply to be read.")
    if not isinstance(error, yaml.YAMLError):
        return Problem(
            top_level(),
            f"The file is not valid YAML: {error}. If a value is meant as plain text, "
            "put it in quotes.",
        )
    said = getattr(error, "problem", None) or str(error)
    explanation = next((plain for part, plain in _YAML_EXPLANATIONS if part in said), None)
    message = "The file is not valid YAML. " + (explanation or f"YAML reports: {said}.")
    mark = getattr(error, "problem_mark", None)
    started = getattr(error, "context_mark", None)
    if started is not None and any(part in said for part in _REPORT_WHERE_IT_STARTED):
        mark = started
    if mark is None:
        return Problem(top_level(), message)
    line = mark.line + 1
    lines = text.splitlines()
    if line <= len(lines) and lines[line - 1].strip():
        message += f" Line {line} reads: {lines[line - 1].strip()}"
    return Problem(f"line {line}", message)


def _read_source(source: object) -> tuple[Any, list[Problem]]:
    """Turn YAML text or a mapping into the document, plus any problems reading it."""
    if isinstance(source, Mapping):
        return dict(source), []
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError:
            return None, [Problem(top_level(), "The file is not text in UTF-8 encoding.")]
    if not isinstance(source, str):
        return None, [
            Problem(
                top_level(),
                f"The config must be YAML text or a group of keys, not {describe(source)}.",
            )
        ]
    loader = None
    try:
        loader = _Loader(source)
        data = loader.get_single_data()
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as error:
        # ValueError is what PyYAML raises for a date that does not exist, like 2024-13-45.
        return None, [_yaml_problem(error, source)]
    finally:
        if loader is not None:
            loader.dispose()
    repeated = [
        Problem(
            f"line {line}",
            f"The key {describe(key)} is written twice in the same group. Only the last one "
            "would be used. Remove one of them, or rename it.",
        )
        for key, line in loader.repeated_keys
    ]
    return data, repeated


# ----------------------------------------------------------------------- the checks


def validate_config(
    source: str | bytes | Mapping[str, Any], context: ValidationContext | None = None
) -> ConfigReport:
    """Check a config and report every problem.

    ``source`` is YAML text or an already-parsed mapping. This never raises for bad input.
    """
    data, errors = _read_source(source)
    if data is None and not errors:
        errors = [
            Problem(
                top_level(),
                "The file is empty. A workflow config needs `workflow`, `dataset`, "
                "`label_task` and `stages`.",
            )
        ]
    elif data is not None and not isinstance(data, Mapping):
        errors = [
            Problem(
                top_level(),
                "The config must be a group of keys (`workflow`, `dataset`, `label_task`, "
                f"`stages`, `final`), but the file holds {describe(data)}.",
            )
        ]
    if not isinstance(data, Mapping):
        return ConfigReport(None, errors)

    config = None
    try:
        config = WorkflowConfig.model_validate(data)
    except ValidationError as error:
        errors += _problems_from(data, error)
    if config is not None and context is not None:
        errors += check_context(config, context)
    if errors:
        return ConfigReport(None, errors)
    warnings = []
    if config is not None and context is not None and context.roles is not None:
        warnings = role_warnings(config, context.roles)
    return ConfigReport(config, [], warnings)


def _problems_from(data: Mapping[str, Any], error: ValidationError) -> list[Problem]:
    """Plain-language problems for a failed validation.

    Pydantic checks the keys first and the graph rules only when every key is valid. To
    report both kinds together, the graph rules are also run here on the raw document,
    skipping whatever is already known to be wrong.
    """
    field_errors, graph_problems = [], []
    for item in error.errors(include_url=False):
        cause = item.get("ctx", {}).get("error")
        if isinstance(cause, ConfigGraphError):
            graph_problems += cause.problems
        else:
            field_errors.append(item)
    if not field_errors:
        return graph_problems
    problems, places = translate_errors(data, field_errors)
    problems += check_graph(data, places)
    return problems or [Problem(top_level(), "The config is not valid.")]


def check_context(config: WorkflowConfig, context: ValidationContext) -> list[Problem]:
    """Errors for a role, dataset or label task the project does not have."""
    problems = []
    if context.datasets is not None:
        problems += _unknown_name(
            top_level("dataset"), "dataset", config.dataset, context.datasets, "datasets"
        )
    if context.label_tasks is not None:
        problems += _unknown_name(
            top_level("label_task"),
            "label task",
            config.label_task,
            context.label_tasks,
            "label tasks",
        )
    if context.roles is not None:
        for position, stage in enumerate(config.stages, start=1):
            where = stage_where(stage.name, position, "assign_to")
            for role in stage.roles:
                problems += _unknown_name(where, "role", role, context.roles, "roles")
    return problems


def _unknown_name(
    where: str, noun: str, name: str, known: Collection[str], plural_noun: str
) -> list[Problem]:
    """An error when ``name`` is not among the ``known`` names, else nothing."""
    if name in known:
        return []
    options = sorted(known)
    if options:
        help_text = suggestion(name, options) or f"The {plural_noun} are {quoted_list(options)}."
    else:
        help_text = f"The project has no {plural_noun} yet."
    return [
        Problem(where, sentences(f"The {noun} '{name}' does not exist in this project.", help_text))
    ]


# --------------------------------------------------------------------- role warnings


def role_warnings(config: WorkflowConfig, roles: Mapping[str, Collection[str]]) -> list[Problem]:
    """Warn about stages whose roles are held by too few people to fill their slots.

    ``roles`` maps each role to the ids of the people holding it now. A stage needs as many
    different people as its ``labels_per_item`` (the ``count``, for ``{count, fraction}``),
    or at least one person for ``all``. Stages naming a role that does not exist are
    skipped: that is an error, which ``validate_config`` reports.
    """
    warnings = []
    for position, stage in enumerate(config.stages, start=1):
        if any(role not in roles for role in stage.roles):
            continue
        holders = {person for role in stage.roles for person in roles[role]}
        message = _role_shortage(stage, len(holders))
        if message:
            warnings.append(Problem(stage_where(stage.name, position), message))
    return warnings


def _role_shortage(stage: Stage, holders: int) -> str | None:
    """The warning for a stage whose roles have this many holders, or None if that is enough."""
    roles = stage.roles
    the_roles = f"the role '{roles[0]}'" if len(roles) == 1 else f"the roles {quoted_list(roles)}"
    the_role = "the role" if len(roles) == 1 else "one of the roles"
    held = the_roles if len(roles) == 1 else f"any of {the_roles}"

    if stage.labels_per_item == "all":
        if holders:
            return None
        return (
            f"Stage '{stage.name}' has everyone holding {the_roles} label every item, but "
            f"nobody holds {'it' if len(roles) == 1 else 'any of them'} yet, so no item can be "
            f"labeled there. Give {the_role} to the people who should label before starting."
        )

    needed, what = _labels_needed(stage.labels_per_item)
    if holders >= needed:
        return None
    if holders == 0:
        return (
            f"Stage '{stage.name}' needs {needed} different people {what}, but nobody holds "
            f"{held}. Its slots will wait until people are given {the_role}."
        )
    who = "only 1 person holds" if holders == 1 else f"only {holders} people hold"
    shortfall = needed - holders
    more = "one more person" if shortfall == 1 else f"{shortfall} more people"
    return (
        f"Stage '{stage.name}' needs {needed} different people {what}, but {who} {held}. "
        "Some items will wait for another person, because nobody labels the same item twice. "
        f"Give {the_role} to {more}, or lower `labels_per_item`."
    )


def _labels_needed(labels: Any) -> tuple[int, str]:
    """The number of different people a stage can need for one item, and how to say it."""
    if isinstance(labels, int):
        return labels, "to label each item"
    return labels.count, (
        f"for the {percent(labels.fraction)} of items chosen to get {labels.count} labels"
    )


__all__ = [
    "ConfigReport",
    "ValidationContext",
    "check_context",
    "role_warnings",
    "validate_config",
]
