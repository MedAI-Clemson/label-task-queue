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


class Paused(NotAllowed):
    """The slot's stage, or its whole workflow, is paused: nothing in it can be claimed."""


class SlotNotYours(NotAllowed):
    """The slot is not currently claimed by the person acting on it."""


class EditRefused(NotAllowed):
    """A proposed config edit cannot be activated. ``problems`` lists every reason.

    Either the config is not valid, or a change needs a new workflow (or must wait, such as
    removing a stage that still has open or claimed slots). The change preview shows the
    same reasons.
    """

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("The edit was not activated: " + " ".join(self.problems))


class VersionConflict(NotAllowed):
    """The edit was previewed against a config version that is no longer the active one."""


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
