"""Errors the engine raises when a request cannot be carried out.

Every message is plain language, written for the person who made the request: it says what
was refused and why. A refused request appends no events.
"""

from __future__ import annotations

from collections.abc import Sequence


class EngineError(Exception):
    """A request the engine refused. ``str(error)`` is a plain-language explanation."""


class NotFound(EngineError):
    """The request names something that does not exist."""


class UnknownRole(NotFound):
    pass


class UnknownDataset(NotFound):
    pass


class UnknownItem(NotFound):
    pass


class UnknownWorkflow(NotFound):
    pass


class UnknownStage(NotFound):
    pass


class UnknownSlot(NotFound):
    pass


class UnknownLabelTaskRef(NotFound):
    """The workflow config names a label task that is not registered."""


class AlreadyExists(EngineError):
    """The request would create something that already exists."""


class NotAllowed(EngineError):
    """The request is well formed but not allowed in the current state."""


class NotEligible(NotAllowed):
    """The person may not work on this slot."""


class SlotNotYours(NotAllowed):
    """The slot is not currently claimed by the person acting on it."""


class InvalidRequest(EngineError):
    """A request argument is malformed (an empty reason, a bad name, ...)."""


class InvalidLabel(EngineError):
    """The label task rejected a submitted label. ``problems`` lists what is wrong."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("The label was rejected: " + "; ".join(self.problems))


class InvalidShown(EngineError):
    """What the interface reports having shown does not match the item or its labels."""


class BrokenChain(EngineError):
    """The event log's fingerprint chain does not verify: an event was changed or removed."""
