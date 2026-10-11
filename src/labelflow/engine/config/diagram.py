"""Draw a config as a Mermaid flowchart.

The diagram is read-only and always drawn from the config, so it cannot disagree with it.
The output is deterministic: the same config always gives the same text.
"""

from __future__ import annotations

from labelflow.engine.config.models import LabelsFraction, Stage, WorkflowConfig
from labelflow.engine.config.problems import percent

# Characters that Mermaid treats as syntax inside a quoted label, and their entity codes.
# "#" comes first so that the codes added for the others are not escaped again.
_ESCAPES = (
    ("#", "#35;"),
    ('"', "#quot;"),
    ("<", "#lt;"),
    (">", "#gt;"),
    ("&", "#38;"),
    ("|", "#124;"),
    ("`", "#96;"),
    ("\n", " "),
)


def escape(text: str) -> str:
    """Make text safe inside a quoted Mermaid label."""
    for character, code in _ESCAPES:
        text = text.replace(character, code)
    return text


def ordinal(number: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', 21 -> '21st'."""
    if number % 100 in (11, 12, 13):
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def to_mermaid(config: WorkflowConfig) -> str:
    """A Mermaid ``flowchart`` of the workflow: dataset, stages, and the final step."""
    ids = {stage.name: f"stage{index}" for index, stage in enumerate(config.stages, start=1)}
    lines = ["flowchart TD", f'    dataset(["{escape("Dataset: " + config.dataset)}"])']
    for stage in config.stages:
        lines.append(f'    {ids[stage.name]}["{_stage_label(stage)}"]')

    final = config.resolved_final()
    if final is None:
        lines.append('    final["no final labels"]')
    else:
        lines.append('    final(["Final label"])')

    for stage in config.stages:
        ref = stage.input_ref
        source = "dataset" if ref is None else ids[ref.stage]
        outcome = None if ref is None or ref.outcome == "all" else ref.outcome
        sample = percent(stage.sample) + " sample" if stage.sample < 1 else None
        label = ", ".join(part for part in (outcome, sample) if part)
        lines.append(_edge(source, ids[stage.name], label))

    for place, name in enumerate(final or (), start=1):
        lines.append(_edge(ids[name], "final", ordinal(place)))

    if final is None:
        lines.append("    classDef note stroke-dasharray: 4 4")
        lines.append("    class final note")
    return "\n".join(lines) + "\n"


def _edge(source: str, target: str, label: str | None) -> str:
    arrow = f'-->|"{escape(label)}"|' if label else "-->"
    return f"    {source} {arrow} {target}"


def _stage_label(stage: Stage) -> str:
    """The text inside a stage's box: name, roles, labels per item, and special settings."""
    roles = ", ".join(stage.roles)
    parts = [
        stage.name,
        f"{'role' if len(stage.roles) == 1 else 'roles'}: {roles}",
        _labels_text(stage.labels_per_item),
    ]
    if not stage.show_prior:
        parts.append("blind")
    if stage.score_against:
        parts.append(f"scored against {stage.score_against}")
    return "<br/>".join(escape(part) for part in parts)


def _labels_text(labels: int | str | LabelsFraction) -> str:
    if labels == "all":
        return "every role holder labels each item"
    if isinstance(labels, LabelsFraction):
        return f"{labels.count} labels for {percent(labels.fraction)} of items, else 1"
    return "1 label per item" if labels == 1 else f"{labels} labels per item"
