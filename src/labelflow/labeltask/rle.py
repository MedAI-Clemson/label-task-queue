"""Run-length encoding (RLE) of binary masks, in the COCO format.

A mask is stored as a small JSON-compatible dict::

    {"size": [height, width], "counts": ...}

The pixels are read in column-major (Fortran) order: down the first column, then down the
second, and so on. ``counts`` lists the lengths of the runs, starting with a run of
background (zeros), which may have length 0 when the mask starts with foreground. Runs
alternate from there: background, foreground, background, and so on.

``counts`` comes in two forms that mean the same thing:

- **uncompressed**: a list of run lengths, for example ``[0, 4]`` for a 2 x 2 mask that is all
  foreground;
- **compressed**: the COCO string, for example ``"04"`` for the same mask.

The compressed form follows ``rleToString`` and ``rleFrString`` in pycocotools' ``maskApi.c``:
each number is written in groups of 5 bits, one character per group (character code 48 plus
the group, plus 32 when more groups follow), and from the fourth number on, each number is
stored as the difference from the number two places before it. Masks written here can be read
by COCO tools, and the other way round.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from labelflow.labeltask._messages import describe_kind

RLE = dict[str, Any]
"""A mask as ``{"size": [height, width], "counts": list or string}``."""

_FIRST_CODE = 48  # the character code of the lowest compressed digit
_CONTINUE_BIT = 0x20  # more 5-bit groups follow
_SIGN_BIT = 0x10  # the top bit of the last 5-bit group says the number is negative


class RLEError(ValueError):
    """A mask is not a valid COCO RLE. ``problems`` says what is wrong, in plain language."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("Not a valid COCO RLE: " + "; ".join(self.problems))


# --- Counts as a string -------------------------------------------------------------------


def counts_to_string(counts: Sequence[int]) -> str:
    """Write run lengths in the COCO compressed form (``rleToString``)."""
    characters = []
    for i, count in enumerate(counts):
        number = count
        if i > 2:
            number -= counts[i - 2]
        more = True
        while more:
            group = number & 0x1F  # the lowest 5 bits
            number >>= 5  # an arithmetic shift, so negative numbers end at -1
            # Stop once what is left is all zeros (or, for a negative number, all ones).
            more = number != -1 if group & _SIGN_BIT else number != 0
            if more:
                group |= _CONTINUE_BIT
            characters.append(chr(group + _FIRST_CODE))
    return "".join(characters)


def counts_from_string(text: str) -> list[int]:
    """Read run lengths from the COCO compressed form (``rleFrString``)."""
    counts: list[int] = []
    position = 0
    while position < len(text):
        number = 0
        shift = 0
        while True:
            if position >= len(text):
                raise RLEError(["'counts' ends in the middle of a number"])
            code = ord(text[position]) - _FIRST_CODE
            if not 0 <= code < 64:
                raise RLEError(
                    [
                        f"'counts' contains the character {text[position]!r} (position "
                        f"{position + 1}), which compressed COCO counts never use"
                    ]
                )
            position += 1
            number |= (code & 0x1F) << shift
            shift += 5
            if not code & _CONTINUE_BIT:
                if code & _SIGN_BIT:
                    number |= -1 << shift  # a negative number: fill the upper bits with ones
                break
        if len(counts) > 2:
            number += counts[-2]
        if number < 0:
            raise RLEError([f"'counts' has a negative run length at entry {len(counts) + 1}"])
        counts.append(number)
    return counts


# --- Checking a mask ----------------------------------------------------------------------


def problems(rle: object) -> list[str]:
    """Everything wrong with ``rle`` as a mask, in plain language; empty if it is valid.

    The messages are phrases such as ``"'size' must be ..."``, meant to follow a lead-in like
    "Mask 'lung' is not a valid COCO RLE:".
    """
    return _read(rle)[2]


def validate(rle: object) -> None:
    """Raise RLEError if ``rle`` is not a valid mask. The mask itself is not changed."""
    if found := problems(rle):
        raise RLEError(found)


def _read(rle: object) -> tuple[tuple[int, int] | None, list[int] | None, list[str]]:
    """Return ``(size, run lengths, problems)``; size or run lengths are None if unreadable."""
    if not isinstance(rle, dict):
        kind = describe_kind(rle)
        return None, None, [f"it must be an object with 'size' and 'counts', not {kind}"]
    found = [
        f"unexpected key {key!r} (only 'size' and 'counts' are allowed)"
        for key in rle
        if key not in ("size", "counts")
    ]
    size = _read_size(rle, found)
    counts = _read_counts(rle, found)
    if size is not None and counts is not None:
        pixels = size[0] * size[1]
        if sum(counts) != pixels:
            found.append(
                f"'counts' add up to {sum(counts)} pixels, but a {size[0]} x {size[1]} mask "
                f"has {pixels}"
            )
    return size, counts, found


def _read_size(rle: dict, found: list[str]) -> tuple[int, int] | None:
    if "size" not in rle:
        found.append("it has no 'size' (give [height, width])")
        return None
    size = rle["size"]
    if (
        isinstance(size, list | tuple)
        and len(size) == 2
        and all(_is_whole_number(side) and side >= 1 for side in size)
    ):
        return size[0], size[1]
    found.append(f"'size' must be two whole numbers [height, width], each at least 1, not {size!r}")
    return None


def _read_counts(rle: dict, found: list[str]) -> list[int] | None:
    if "counts" not in rle:
        found.append("it has no 'counts'")
        return None
    counts = rle["counts"]
    if isinstance(counts, str):
        try:
            return counts_from_string(counts)
        except RLEError as error:
            found.extend(error.problems)
            return None
    if isinstance(counts, list | tuple):
        if all(_is_whole_number(count) and count >= 0 for count in counts):
            return list(counts)
        found.append("'counts' must contain only whole numbers that are 0 or more")
        return None
    kind = describe_kind(counts)
    found.append(f"'counts' must be a list of run lengths or a compressed string, not {kind}")
    return None


def _is_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse(rle: object) -> tuple[tuple[int, int], list[int]]:
    """Return the size and the run lengths of a valid mask, or raise RLEError."""
    size, counts, found = _read(rle)
    if found or size is None or counts is None:
        raise RLEError(found)
    return size, counts


# --- Encoding, decoding, converting -------------------------------------------------------


def encode(mask: np.ndarray, compressed: bool = True) -> RLE:
    """Encode a 2-D mask (height x width, any non-zero value is foreground) as a COCO RLE."""
    mask = np.asarray(mask)
    if mask.ndim != 2 or mask.size == 0:
        raise ValueError("A mask must be a 2-D array with at least one pixel.")
    height, width = mask.shape
    pixels = mask.astype(bool).ravel(order="F")  # column-major, as COCO reads it

    # Runs end where a pixel differs from the one before it.
    run_starts = np.flatnonzero(pixels[1:] != pixels[:-1]) + 1
    edges = np.concatenate(([0], run_starts, [pixels.size]))
    counts = np.diff(edges).tolist()
    if pixels[0]:
        counts.insert(0, 0)  # the first run is always background, even if it is empty

    return {
        "size": [height, width],
        "counts": counts_to_string(counts) if compressed else counts,
    }


def decode(rle: RLE) -> np.ndarray:
    """Decode a COCO RLE (either form) to a boolean array of shape (height, width)."""
    (height, width), counts = _parse(rle)
    is_foreground = np.arange(len(counts)) % 2 == 1  # runs alternate, starting with background
    pixels = np.repeat(is_foreground, counts)
    return pixels.reshape((height, width), order="F")


def area(rle: RLE) -> int:
    """The number of foreground pixels: the sum of every second run (``rleArea``)."""
    _, counts = _parse(rle)
    return sum(counts[1::2])


def to_uncompressed(rle: RLE) -> RLE:
    """The same mask with ``counts`` as a list of run lengths. Returns a new dict."""
    (height, width), counts = _parse(rle)
    return {"size": [height, width], "counts": counts}


def to_compressed(rle: RLE) -> RLE:
    """The same mask with ``counts`` as the COCO compressed string. Returns a new dict."""
    (height, width), counts = _parse(rle)
    return {"size": [height, width], "counts": counts_to_string(counts)}


def mask_iou(first: np.ndarray, second: np.ndarray) -> float:
    """Intersection over union of two boolean masks of the same shape.

    Two empty masks have an IoU of 1.0 (they agree). An empty mask and a non-empty one have 0.
    """
    if first.shape != second.shape:
        raise ValueError("Masks of different sizes cannot be compared.")
    union = int(np.count_nonzero(first | second))
    if union == 0:
        return 1.0
    return int(np.count_nonzero(first & second)) / union
