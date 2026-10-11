"""A stand-in label task for tests and simulations.

A label is ``{"value": <non-empty string>}``. Labels agree when all their values are equal,
and the agreed label is a copy of the first. Simulations decide how often labels agree by
choosing which values to submit.
"""

from __future__ import annotations

from collections.abc import Sequence

from labelflow.labeltask.base import ConsensusResult, Label, LabelTask, LabelValidationError


class StandInLabelTask(LabelTask):
    """Labels are ``{"value": str}``; consensus means every value is equal."""

    def __init__(self, name: str = "standin", version: int = 1, rule: str = "equal-values@1"):
        self.name = name
        self.version = version
        self._rule = rule

    @property
    def ontology_version(self) -> str:
        return f"{self.name}-ontology@{self.version}"

    @property
    def consensus_rule_version(self) -> str:
        return self._rule

    def validate(self, label: Label) -> Label:
        if not isinstance(label, dict):
            raise LabelValidationError([f"A label must be an object, not {type(label).__name__}."])
        problems = [f"Unknown field '{key}'." for key in sorted(label) if key != "value"]
        value = label.get("value")
        if not isinstance(value, str) or not value:
            problems.append("The field 'value' must be a non-empty string.")
        if problems:
            raise LabelValidationError(problems)
        return {"value": value}

    def check_consensus(self, labels: Sequence[Label]) -> ConsensusResult:
        if len(labels) < 2:
            raise ValueError("The consensus check needs two or more labels.")
        values = [label["value"] for label in labels]
        agreed = all(value == values[0] for value in values)
        return ConsensusResult(
            agreed=agreed,
            agreed_label=dict(labels[0]) if agreed else None,
            detail={"values": values},
            rule_version=self._rule,
        )
