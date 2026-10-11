"""The label task contract: everything about what a label is.

The workflow never opens a label. It asks a label task three things:

- is this submitted label valid? (``validate``)
- do these labels agree? (``check_consensus``; the answer is yes or no)
- does this label agree with a key? (``score``; the same check, run on the label and the key)

A label task is code, named and versioned, registered with a deployment, and referenced
from a workflow config as ``name@version``, for example ``image_ontology@1``.
"""

from __future__ import annotations

import difflib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

Label = dict[str, Any]
"""A label as submitted: a JSON-compatible dict. Its shape belongs to the label task."""


class LabelValidationError(ValueError):
    """A submitted label does not match the label task.

    ``problems`` holds one plain-language message per problem found.
    """

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class ConsensusResult(BaseModel):
    """The answer to "do these labels agree?".

    The workflow reads only ``agreed`` and ``agreed_label``. ``detail`` (per-field
    agree/disagree flags, IoU values and so on) is for the interface; the workflow never
    looks inside it.
    """

    model_config = ConfigDict(frozen=True)

    agreed: bool
    agreed_label: Label | None = None
    """When ``agreed``, the agreed label: a copy of the earliest submitted label."""
    detail: dict[str, Any] = {}
    rule_version: str
    """The version of the consensus rule that produced this answer; logged with it."""


class LabelTask(ABC):
    """A named, versioned label task."""

    name: str
    version: int

    @property
    def ref(self) -> str:
        """The reference used in a workflow config, ``name@version``."""
        return f"{self.name}@{self.version}"

    @property
    @abstractmethod
    def ontology_version(self) -> str:
        """The version of the ontology (label schema); recorded with every label."""

    @property
    @abstractmethod
    def consensus_rule_version(self) -> str:
        """The version of the consensus rule; recorded with every consensus answer."""

    @abstractmethod
    def validate(self, label: Label) -> Label:
        """Check a submitted label and return it in normal form.

        Raises LabelValidationError, with plain-language messages, if it does not match.
        """

    @abstractmethod
    def check_consensus(self, labels: Sequence[Label]) -> ConsensusResult:
        """Decide whether two or more valid labels agree.

        ``labels`` are in submission order, so when they agree the agreed label is a copy
        of ``labels[0]``.
        """

    def score(self, label: Label, key: Label) -> ConsensusResult:
        """Compare a label with a key, such as a gold-standard answer.

        Scoring runs the same consensus check on the label and the key.
        """
        return self.check_consensus([label, key])


class UnknownLabelTask(KeyError):
    """No label task is registered under the requested reference."""

    def __init__(self, ref: str, known: Iterable[str]):
        self.ref = ref
        message = f"No label task named '{ref}' is registered."
        if close := difflib.get_close_matches(ref, list(known), n=1):
            message += f" Did you mean '{close[0]}'?"
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class LabelTaskRegistry:
    """The label tasks registered with a deployment, by ``name@version``."""

    def __init__(self, tasks: Iterable[LabelTask] = ()):
        self._tasks: dict[str, LabelTask] = {}
        for task in tasks:
            self.register(task)

    def register(self, task: LabelTask) -> None:
        if task.ref in self._tasks:
            raise ValueError(f"A label task named '{task.ref}' is already registered.")
        self._tasks[task.ref] = task

    def get(self, ref: str) -> LabelTask:
        try:
            return self._tasks[ref]
        except KeyError:
            raise UnknownLabelTask(ref, self._tasks) from None

    def __contains__(self, ref: object) -> bool:
        return ref in self._tasks

    def refs(self) -> list[str]:
        return sorted(self._tasks)
