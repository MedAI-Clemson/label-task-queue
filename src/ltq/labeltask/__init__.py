"""Label tasks: the ontology, validating labels, and the consensus check."""

from ltq.labeltask.base import (
    ConsensusResult,
    Label,
    LabelTask,
    LabelTaskRegistry,
    LabelValidationError,
    UnknownLabelTask,
)

__all__ = [
    "ConsensusResult",
    "Label",
    "LabelTask",
    "LabelTaskRegistry",
    "LabelValidationError",
    "UnknownLabelTask",
]
