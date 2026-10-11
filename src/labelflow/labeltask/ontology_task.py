"""The built-in label task: labels checked against an ontology file.

A label is a JSON-compatible dict with two parts, both optional in the dict itself but
required by the ontology where it says so::

    {"classifications": {"view": "PA", "quality": "good"},
     "masks": {"lung": {"size": [512, 512], "counts": "..."},
               "effusion": {"size": [512, 512], "counts": [262144]}}}

Masks are COCO RLE (see ``rle.py``), one per mask class. An empty mask is an RLE with no
foreground, such as ``{"size": [512, 512], "counts": [262144]}``. A mask may have at most
``max_mask_pixels`` pixels (an ontology setting; 64 megapixels by default), so that a label
can never make the server decode more than it has memory for. The consensus check compares
masks by their run lists, without decoding them at all.

Labels are stored exactly as submitted: ``validate`` checks a label and returns a copy of it,
and never re-encodes or tidies it.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from labelflow.labeltask import rle
from labelflow.labeltask.base import (
    ConsensusResult,
    Label,
    LabelTask,
    LabelTaskRegistry,
    LabelValidationError,
    describe_kind,
    did_you_mean,
    quoted,
)
from labelflow.labeltask.ontology import (
    Classification,
    MaskClass,
    Ontology,
    OntologyError,
    parse_ontology,
)

CONSENSUS_RULE_VERSION = "ontology-consensus@1"
"""Bump this if the rule below ever changes, so old answers stay interpretable."""

LABEL_KEYS = ("classifications", "masks")

_EMPTY_MASK_HINT = (
    "Every mask class must be present; for an empty mask, give an RLE with no foreground, "
    'such as {"size": [512, 512], "counts": [262144]} for a 512 x 512 image.'
)


class OntologyLabelTask(LabelTask):
    """A label task defined by an ontology: classifications and one mask per mask class."""

    def __init__(self, ontology: Ontology):
        self.ontology = ontology
        self.name = ontology.name
        self.version = ontology.version
        self._classifications = {item.name: item for item in ontology.classifications}
        self._mask_classes = {item.name: item for item in ontology.masks}
        self._max_pixels = ontology.mask_pixel_cap

    @property
    def ontology_version(self) -> str:
        """``name@version+fingerprint``; the fingerprint changes when the ontology is edited."""
        return f"{self.ref}+{self.ontology.fingerprint()}"

    @property
    def consensus_rule_version(self) -> str:
        return CONSENSUS_RULE_VERSION

    # --- Validation -----------------------------------------------------------------------

    def validate(self, label: Label) -> Label:
        """Check a label against the ontology; return a deep copy, equal to what was submitted.

        Raises LabelValidationError listing every problem found.
        """
        if not isinstance(label, dict):
            kind = describe_kind(label)
            raise LabelValidationError(
                [f"A label must be an object with 'classifications' and 'masks', not {kind}."]
            )
        problems = [
            f"Unknown key {key!r} in the label.{did_you_mean(key, LABEL_KEYS)} "
            f"A label has only {quoted(LABEL_KEYS)}."
            for key in label
            if key not in LABEL_KEYS
        ]
        problems += self._classification_problems(label.get("classifications"))
        problems += self._mask_problems(label.get("masks"))
        if problems:
            raise LabelValidationError(problems)
        return copy.deepcopy(label)

    def _classification_problems(self, classifications: object) -> list[str]:
        if classifications is None:
            classifications = {}
        if not isinstance(classifications, dict):
            kind = describe_kind(classifications)
            return [f"'classifications' must be an object of name and value pairs, not {kind}."]

        problems = []
        for name, value in classifications.items():
            spec = self._classifications.get(name)
            if spec is None:
                problems.append(self._unknown_name(name, "classifications"))
            elif value is not None and value not in spec.values:
                problems.append(
                    f"Classification {name!r} has the value {value!r}, which is not allowed."
                    f"{did_you_mean(value, spec.values)} Allowed values: {quoted(spec.values)}."
                )
        for spec in self.ontology.classifications:
            if spec.required and classifications.get(spec.name) is None:
                problems.append(
                    f"Required classification {spec.name!r} is missing. "
                    f"Allowed values: {quoted(spec.values)}."
                )
        return problems

    def _mask_problems(self, masks: object) -> list[str]:
        if masks is None:
            masks = {}
        if not isinstance(masks, dict):
            kind = describe_kind(masks)
            return [f"'masks' must be an object that gives an RLE for each mask class, not {kind}."]

        problems = []
        sizes: dict[str, tuple[int, int]] = {}  # of the masks that are valid RLEs
        for name, mask in masks.items():
            if name not in self._mask_classes:
                problems.append(self._unknown_name(name, "masks"))
            elif found := rle.problems(mask, self._max_pixels):
                lead_in = f"Mask {name!r} is not a valid COCO RLE"
                problems += [f"{lead_in}: {problem}." for problem in found]
            else:
                sizes[name] = tuple(mask["size"])
        for name in self._mask_classes:
            if name not in masks:
                problems.append(f"The mask for {name!r} is missing. {_EMPTY_MASK_HINT}")
        if len(set(sizes.values())) > 1:
            listing = ", ".join(f"{name!r} is {h} x {w}" for name, (h, w) in sizes.items())
            problems.append(
                f"The masks are not all the same size: {listing}. "
                "Every mask in a label must have the same size."
            )
        return problems

    def _unknown_name(self, name: object, section: str) -> str:
        """Say that a name is not in the ontology, with a suggestion where possible."""
        if section == "classifications":
            kind, plural, known = "classification", "classifications", self._classifications
            if name in self._mask_classes:
                return f"{name!r} is a mask class, not a classification. Put it under 'masks'."
        else:
            kind, plural, known = "mask class", "mask classes", self._mask_classes
            if name in self._classifications:
                return (
                    f"{name!r} is a classification, not a mask class. "
                    "Put it under 'classifications'."
                )
        message = f"Unknown {kind} {name!r}."
        if suggestion := did_you_mean(name, known):
            return message + suggestion
        if known:
            return f"{message} The {plural} in this ontology are: {quoted(known)}."
        return f"{message} This ontology has no {plural}."

    # --- Consensus ------------------------------------------------------------------------

    def check_consensus(self, labels: Sequence[Label]) -> ConsensusResult:
        """Decide whether two or more valid labels agree.

        Classifications agree when their values match exactly (absent and null count as the
        same). A mask class agrees when the IoU of every pair of masks is at or above the
        class threshold; two empty masks agree, and masks of different sizes disagree. The
        labels agree only if every field does. The agreed label is a copy of ``labels[0]``.

        The labels must already be valid (see ``validate``).
        """
        if len(labels) < 2:
            raise ValueError(f"A consensus check needs at least two labels, but got {len(labels)}.")

        classifications = {
            item.name: _compare_classification(item, labels)
            for item in self.ontology.classifications
        }
        masks = {
            item.name: _compare_masks(item, labels, self._max_pixels)
            for item in self.ontology.masks
        }
        agreed = all(field["agreed"] for field in (*classifications.values(), *masks.values()))
        return ConsensusResult(
            agreed=agreed,
            agreed_label=copy.deepcopy(labels[0]) if agreed else None,
            detail={"classifications": classifications, "masks": masks},
            rule_version=self.consensus_rule_version,
        )

    def score(self, label: Label, key: Label) -> ConsensusResult:
        """Compare a label with a key (consensus on the two), after checking the key is valid."""
        try:
            self.validate(key)
        except LabelValidationError as error:
            raise LabelValidationError(
                [f"The key is invalid. {problem}" for problem in error.problems]
            ) from None
        return super().score(label, key)


def _compare_classification(item: Classification, labels: Sequence[Label]) -> dict[str, Any]:
    values = [_part(label, "classifications").get(item.name) for label in labels]
    return {"agreed": all(value == values[0] for value in values), "values": values}


def _compare_masks(item: MaskClass, labels: Sequence[Label], max_pixels: int) -> dict[str, Any]:
    """Compare one mask class across labels.

    The result has ``agreed``, the class ``threshold``, ``min_iou`` (the lowest pairwise
    IoU), ``pairwise_iou`` (a list of ``[i, j, iou]`` for the labels at positions i and j),
    and ``sizes`` (the ``[height, width]`` of each label's mask). If the masks are not all
    the same size they disagree, and ``min_iou`` and the IoU of each mismatched pair are
    None, since an IoU is not defined between masks of different sizes.

    The sizes are compared first, and the IoU of a pair of the same size is computed from
    the run lists (``rle.counts_iou``); no mask is ever decoded pixel by pixel.
    """
    encoded = [_part(label, "masks")[item.name] for label in labels]
    sizes = [list(mask["size"]) for mask in encoded]
    runs = [rle.to_uncompressed(mask, max_pixels)["counts"] for mask in encoded]

    pairwise = [
        (i, j, rle.counts_iou(runs[i], runs[j]) if sizes[i] == sizes[j] else None)
        for i, j in combinations(range(len(labels)), 2)
    ]
    ious = [iou for _, _, iou in pairwise]
    lowest = None if None in ious else min(ious)
    # Agreement uses the exact IoU; only the reported numbers are rounded.
    return {
        "agreed": lowest is not None and lowest >= item.threshold,
        "threshold": item.threshold,
        "min_iou": _rounded(lowest),
        "pairwise_iou": [[i, j, _rounded(iou)] for i, j, iou in pairwise],
        "sizes": sizes,
    }


def _part(label: Label, section: str) -> dict[str, Any]:
    """The ``classifications`` or ``masks`` part of a label; empty if absent or null."""
    return label.get(section) or {}


def _rounded(iou: float | None) -> float | None:
    return None if iou is None else round(iou, 6)


# --- Loading ontology files ---------------------------------------------------------------


def load_ontology(source: str | Path) -> OntologyLabelTask:
    """Build a label task from an ontology file (a ``Path``) or YAML text (a string).

    A string with a newline in it is read as YAML text; any other string is a file name.
    Raises OntologyError, in plain language, if the ontology is not valid.
    """
    return OntologyLabelTask(parse_ontology(source))


def registry_from_directory(directory: str | Path) -> LabelTaskRegistry:
    """Load every ``*.yaml`` ontology file in a folder into a registry.

    Problems in several files are reported together in one OntologyError.
    """
    directory = Path(directory)
    where = f"folder {directory.name!r}"
    if not directory.is_dir():
        raise OntologyError([f"There is no folder at {str(directory)!r}."], where)

    tasks: list[OntologyLabelTask] = []
    defined_in: dict[str, str] = {}  # ref -> the file that defined it first
    problems: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            task = load_ontology(path)
        except OntologyError as error:
            problems += [f"{path.name}: {problem}" for problem in error.problems]
            continue
        if task.ref in defined_in:
            problems.append(
                f"{path.name}: {task.ref!r} is already defined in {defined_in[task.ref]}. "
                "Each ontology file needs its own name and version."
            )
            continue
        defined_in[task.ref] = path.name
        tasks.append(task)
    if problems:
        raise OntologyError(problems, where)
    return LabelTaskRegistry(tasks)
