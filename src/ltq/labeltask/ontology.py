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

from ltq.labeltask._messages import describe_kind, did_you_mean, quoted

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


class _FileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Classification(_FileModel):
    """A choice from a fixed list of values, such as the view of a chest X-ray."""

    name: StrictStr
    values: tuple[StrictStr, ...]
    required: StrictBool = True
    """A required classification must be answered; an optional one may be left out."""

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, name: str) -> str:
        return _check_not_blank(name, "name")

    @field_validator("values")
    @classmethod
    def _values_are_usable(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("values must list at least one allowed value")
        if any(not value.strip() for value in values):
            raise ValueError("values must not contain an empty or blank value")
        repeated = [value for value, n in Counter(values).items() if n > 1]
        if repeated:
            raise ValueError(f"values lists {quoted(repeated)} more than once")
        return values


class MaskClass(_FileModel):
    """A class that is labeled with a mask, such as the lungs."""

    name: StrictStr
    threshold: Annotated[float, Strict()]
    """The IoU at or above which two masks of this class agree."""

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, name: str) -> str:
        return _check_not_blank(name, "name")

    @field_validator("threshold")
    @classmethod
    def _threshold_is_a_fraction(cls, threshold: float) -> float:
        if not 0 < threshold <= 1:
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

    @model_validator(mode="after")
    def _names_are_unique_and_something_is_labeled(self) -> Ontology:
        names = [item.name for item in (*self.classifications, *self.masks)]
        if not names:
            raise ValueError(
                "The ontology has no classifications and no masks, so there is nothing to label."
            )
        repeated = [name for name, n in Counter(names).items() if n > 1]
        if repeated:
            raise ValueError(
                f"Names must be unique across classifications and masks, but {quoted(repeated)} "
                "appears more than once."
            )
        return self

    def canonical_json(self) -> str:
        """The ontology as JSON with a fixed layout, so that equal ontologies give equal text."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        """The first 12 hex characters of the SHA-256 of the canonical JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:12]


def _check_not_blank(text: str, what: str) -> str:
    if not text.strip():
        raise ValueError(f"{what} must not be empty")
    return text


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
        raise OntologyError(
            [_plain_problem(item, data) for item in error.errors()], source
        ) from None


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe YAML loader that reports a repeated key, where plain YAML keeps the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None,
                        None,
                        f"the key {key!r} appears more than once",
                        key_node.start_mark,
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


def _plain_problem(item: Any, data: dict[str, Any]) -> str:
    """Write one pydantic error as a sentence that says where the problem is."""
    location = tuple(item["loc"])

    if item["type"] == "extra_forbidden":
        place, message = location[:-1], _unknown_key(location[:-1], str(location[-1]))
    elif location and isinstance(location[-1], int):  # one entry of a list
        subject = f"item {location[-1] + 1} of {location[-2]!r}"
        place, message = location[:-2], _what_is_wrong(item, subject)
    elif location:
        place, message = location[:-1], _what_is_wrong(item, repr(location[-1]))
    else:
        place, message = (), _what_is_wrong(item, "")

    where = _where(place, data)
    return f"{where}: {message}" if where else message[:1].upper() + message[1:]


def _where(place: tuple[Any, ...], data: dict[str, Any]) -> str:
    """Name the classification or mask class that a problem is in, such as "mask class 'lung'"."""
    if not place:
        return ""
    section, index = place  # for example ("masks", 1)
    noun = "classification" if section == "classifications" else "mask class"
    entries = data.get(section)
    entry = entries[index] if isinstance(entries, list) and index < len(entries) else None
    name = entry.get("name") if isinstance(entry, dict) else None
    return f"{noun} {name!r}" if isinstance(name, str) else f"{noun} #{index + 1}"


def _unknown_key(place: tuple[Any, ...], key: str) -> str:
    """Say that a key is unknown, with a suggestion and the keys that are allowed."""
    if not place:
        allowed = list(Ontology.model_fields)
    elif place[0] == "classifications":
        allowed = list(Classification.model_fields)
    else:
        allowed = list(MaskClass.model_fields)
    return (
        f"unknown key {key!r}.{did_you_mean(key, allowed)} The allowed keys are {quoted(allowed)}."
    )


def _what_is_wrong(item: Any, subject: str) -> str:
    """The plain-language reason for one pydantic error, as a sentence about ``subject``."""
    kind = item["type"]
    if kind == "value_error":
        return str(item["ctx"]["error"])
    if kind == "missing":
        return f"{subject} is required but missing."
    if kind == "string_type":
        return (
            f"{subject} must be text. In YAML, put quotes around words such as yes, no, on "
            "and off, and around numbers, if you mean them as text."
        )
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return f"{subject} must be a whole number."
    if kind in ("float_type", "float_parsing"):
        return f"{subject} must be a number."
    if kind == "bool_type":
        return f"{subject} must be true or false."
    if kind in ("tuple_type", "list_type"):
        return f"{subject} must be a list."
    if kind in ("model_type", "dict_type", "model_attributes_type"):
        return f"{subject} must be a set of keys and values (such as name: lung)."
    return f"{subject}: {item['msg']}"
