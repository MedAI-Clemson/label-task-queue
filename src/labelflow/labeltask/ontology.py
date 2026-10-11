"""The ontology file: what a researcher writes to define a built-in label task.

The file is YAML. It names the ontology and gives its version, then lists classifications
(a name, the allowed values, whether each is required) and mask classes (a name and the IoU
threshold at or above which two masks agree)::

    name: image_ontology
    version: 1
    classifications:
      - name: view
        values: [PA, AP, lateral]
        required: true
      - name: quality
        values: [good, poor]
        required: false
    masks:                       # one mask per class (semantic segmentation)
      - name: lung
        threshold: 0.8
      - name: effusion
        threshold: 0.5
    max_mask_pixels: 20000000    # optional: the most pixels (height x width) a mask may have

A label's masks must fit within ``max_mask_pixels`` (by default ``rle.MAX_PIXELS``, 64
megapixels) and within ``rle.MAX_SIDE`` (16384) on each side, since the server decodes a mask
into memory pixel by pixel and the interface draws it on a canvas. Set the key lower for small
images, or higher (up to 16384 x 16384) if the images are bigger and the server has the memory.

The models here reject unknown keys, so a misspelled key is reported instead of ignored. A
bad file raises ``OntologyError``, whose messages are written for the person who wrote the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Strict,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from labelflow.labeltask.base import describe_kind, did_you_mean, quoted
from labelflow.labeltask.rle import MAX_PIXELS, MAX_SIDE

# A workflow refers to a label task as name@version, so the name needs a plain shape.
_ONTOLOGY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class OntologyError(ValueError):
    """An ontology file is not valid. ``problems`` holds one plain-language message each."""

    def __init__(self, problems: Sequence[str], source: str = "given as text"):
        self.problems = list(problems)
        self.source = source
        count = len(self.problems)
        header = f"The ontology ({source}) has {count} problem{'' if count == 1 else 's'}:"
        super().__init__("\n".join([header, *(f"  - {problem}" for problem in self.problems)]))


# --- The models ---------------------------------------------------------------------------


def _not_blank(name: str) -> str:
    if not name.strip():
        raise ValueError("name must not be empty")
    return name


_Name = Annotated[StrictStr, AfterValidator(_not_blank)]


class _FileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Classification(_FileModel):
    """A choice from a fixed list of values, such as the view of a chest X-ray."""

    name: _Name
    values: tuple[StrictStr, ...]
    required: StrictBool = True
    """A required classification must be answered; an optional one may be left out."""

    @field_validator("values")
    @classmethod
    def _values_are_usable(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("values must list at least one allowed value")
        if any(not value.strip() for value in values):
            raise ValueError("values must not contain an empty or blank value")
        if repeated := _repeated(values):
            raise ValueError(f"values lists {quoted(repeated)} more than once")
        return values


class MaskClass(_FileModel):
    """A class that is labeled with a mask, such as the lungs."""

    name: _Name
    threshold: Annotated[float, Strict()]
    """The IoU at or above which two masks of this class agree."""

    @field_validator("threshold")
    @classmethod
    def _threshold_is_a_fraction(cls, threshold: float) -> float:
        if not 0 < threshold <= 1:  # also catches NaN
            raise ValueError(
                f"threshold must be above 0 and at most 1 (it is the IoU at or above which "
                f"two masks agree), but it is {threshold}"
            )
        return threshold


class Ontology(_FileModel):
    """The contents of an ontology file."""

    name: StrictStr
    version: StrictInt
    classifications: tuple[Classification, ...] = ()
    masks: tuple[MaskClass, ...] = ()
    max_mask_pixels: StrictInt | None = None
    """The most pixels (height x width) a mask may have; None means ``rle.MAX_PIXELS``."""

    @field_validator("name")
    @classmethod
    def _name_is_usable_in_a_reference(cls, name: str) -> str:
        if not _ONTOLOGY_NAME.match(name):
            raise ValueError(
                f"name {name!r} must start with a letter and use only letters, digits and "
                "underscores, so that a workflow can refer to it as name@version, for "
                "example image_ontology@1"
            )
        return name

    @field_validator("version")
    @classmethod
    def _version_is_positive(cls, version: int) -> int:
        if version < 1:
            raise ValueError(f"version must be a whole number from 1 up, but it is {version}")
        return version

    @field_validator("max_mask_pixels")
    @classmethod
    def _max_mask_pixels_is_a_usable_cap(cls, cap: int | None) -> int | None:
        if cap is not None and not 1 <= cap <= MAX_SIDE * MAX_SIDE:
            raise ValueError(
                f"max_mask_pixels must be a whole number from 1 to {MAX_SIDE * MAX_SIDE} "
                f"({MAX_SIDE} x {MAX_SIDE}, the largest mask the interface can show), but it "
                f"is {cap}. Leave it out for the default of {MAX_PIXELS} (64 megapixels)"
            )
        return cap

    @property
    def mask_pixel_cap(self) -> int:
        """The cap on a mask's pixels in force: ``max_mask_pixels``, or the default."""
        return MAX_PIXELS if self.max_mask_pixels is None else self.max_mask_pixels

    @model_validator(mode="after")
    def _names_are_unique_and_something_is_labeled(self) -> Ontology:
        names = [item.name for item in (*self.classifications, *self.masks)]
        if not names:
            raise ValueError(
                "The ontology has no classifications and no masks, so there is nothing to label."
            )
        if repeated := _repeated(names):
            raise ValueError(
                f"Names must be unique across classifications and masks, but {quoted(repeated)} "
                "appears more than once."
            )
        return self

    def fingerprint(self) -> str:
        """Twelve hex characters that change whenever the ontology's content changes.

        The SHA-256 is taken over the ontology as JSON with sorted keys and no spaces, so
        comments, key order and layout in the file make no difference. An optional key
        that is left out (``max_mask_pixels``) is left out of the JSON too, so adding such
        a key to the model does not change the fingerprint of files that do not use it.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _repeated(items: Sequence[str]) -> list[str]:
    return [item for item, n in Counter(items).items() if n > 1]


# --- Reading a file -----------------------------------------------------------------------


def parse_ontology(source: str | Path) -> Ontology:
    """Read an ontology from a file (a ``Path``) or from YAML text (a string with a newline).

    A string without a newline is taken to be a file name. Raises OntologyError, with every
    problem found written in plain language, if the file cannot be read or is not valid.
    """
    if isinstance(source, str) and "\n" in source:
        return _parse_text(source, "given as text")

    path = Path(source)
    where = f"file {path.name!r}"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise OntologyError([f"There is no file at {str(path)!r}."], where) from None
    except UnicodeDecodeError:
        raise OntologyError(["The file is not text in the UTF-8 encoding."], where) from None
    except OSError as error:
        raise OntologyError([f"The file could not be read: {error.strerror}."], where) from None
    return _parse_text(text, where)


def _parse_text(text: str, source: str) -> Ontology:
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise OntologyError([_describe_yaml_error(error)], source) from None
    if data is None:
        raise OntologyError(["The file is empty."], source)
    if not isinstance(data, dict):
        raise OntologyError(
            [
                "The file must be a set of keys and values (name, version, classifications, "
                f"masks), but it is {describe_kind(data)}."
            ],
            source,
        )
    try:
        return Ontology.model_validate(data)
    except ValidationError as error:
        problems = [_plain_problem(item, data) for item in error.errors()]
        raise OntologyError(problems, source) from None


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe YAML loader that reports a repeated key, where plain YAML keeps the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"the key {key!r} appears more than once", key_node.start_mark
                    )
                seen.add(key)
            except TypeError:
                pass  # an unhashable key; the base class reports it
        return super().construct_mapping(node, deep)


def _describe_yaml_error(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or str(error)
    where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
    return f"The file could not be read as YAML{where}: {problem}."


# --- Turning pydantic's errors into plain language ----------------------------------------

# What each kind of pydantic error means, as a sentence about the field ("{}"). The models
# use strict types, so these are the kinds that can occur; anything else falls back to
# pydantic's own wording.
_REASONS = {
    "missing": "{} is required but missing.",
    "string_type": (
        "{} must be text. In YAML, put quotes around words such as yes, no, on and off, and "
        "around numbers, if you mean them as text."
    ),
    "int_type": "{} must be a whole number.",
    "float_type": "{} must be a number.",
    "bool_type": "{} must be true or false.",
    "tuple_type": "{} must be a list.",
    "model_type": "{} must be a set of keys and values (such as name: lung).",
}

_ALLOWED_KEYS = {
    "classifications": list(Classification.model_fields),
    "masks": list(MaskClass.model_fields),
}


def _plain_problem(item: Any, data: dict[str, Any]) -> str:
    """Write one pydantic error as a sentence that says where the problem is.

    ``loc`` is a path such as ``("masks", 1, "threshold")``: the entry it belongs to is
    named ("mask class 'lung': ...") and the rest of the path becomes the subject.
    """
    location = tuple(item["loc"])
    if item["type"] == "extra_forbidden":
        entry, message = location[:-1], _unknown_key(location[:-1], str(location[-1]))
    elif location and isinstance(location[-1], int):  # one item of a list
        subject = f"item {location[-1] + 1} of {location[-2]!r}"
        entry, message = location[:-2], _reason(item, subject)
    elif location:
        entry, message = location[:-1], _reason(item, repr(location[-1]))
    else:
        entry, message = (), _reason(item, "")

    if not entry:
        return message[:1].upper() + message[1:]
    section, index = entry
    return f"{_entry_name(section, index, data)}: {message}"


def _entry_name(section: str, index: int, data: dict[str, Any]) -> str:
    """Name a classification or mask class, such as "mask class 'lung'" or "mask class #2"."""
    noun = "classification" if section == "classifications" else "mask class"
    entries = data.get(section)
    entry = entries[index] if isinstance(entries, list) and index < len(entries) else None
    name = entry.get("name") if isinstance(entry, dict) else None
    return f"{noun} {name!r}" if isinstance(name, str) else f"{noun} #{index + 1}"


def _unknown_key(entry: tuple[Any, ...], key: str) -> str:
    """Say that a key is unknown, with a suggestion and the keys that are allowed."""
    allowed = _ALLOWED_KEYS[entry[0]] if entry else list(Ontology.model_fields)
    return (
        f"unknown key {key!r}.{did_you_mean(key, allowed)} The allowed keys are {quoted(allowed)}."
    )


def _reason(item: Any, subject: str) -> str:
    """The plain-language reason for one pydantic error, as a sentence about ``subject``."""
    kind = item["type"]
    if kind == "value_error":  # raised by one of the validators above, already in plain words
        return str(item["ctx"]["error"])
    if kind in _REASONS:
        return _REASONS[kind].format(subject)
    return f"{subject}: {item['msg']}"
