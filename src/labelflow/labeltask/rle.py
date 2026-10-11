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

A mask is small to send but can be huge to decode: seven characters can claim a size of
100000 x 100000, and decoding that would need 10 gigabytes. So a mask's size is capped
(``MAX_SIDE`` on each side and ``max_pixels`` in all, ``MAX_PIXELS`` by default), and the
number of runs is capped at one more than the pixels, which is the most any mask can have.
Agreement between two masks is computed from their run lists (``counts_iou``), without
decoding either.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from labelflow.labeltask.base import describe_kind

RLE = dict[str, Any]
"""A mask as ``{"size": [height, width], "counts": list or string}``."""

MAX_SIDE = 16_384
"""The most pixels a mask may have on either side.

16384 is the largest canvas every major browser can draw, so a mask any wider or taller
could never be shown in the labeling interface, whatever the memory of the server.
"""

MAX_PIXELS = 64 * 1024 * 1024
"""The default cap on a mask's pixels (height x width): 64 megapixels, or 8192 x 8192.

That is above any 2-D medical image labeled whole (a digital mammogram is about 24
megapixels), and a decoded mask of that size is 64 MB, so a handful of masks per request
fits comfortably in memory. An ontology may set its own cap (``max_mask_pixels``).
"""

_FIRST_CODE = 48  # the character code of the lowest compressed digit
_CONTINUE_BIT = 0x20  # more 5-bit groups follow
_SIGN_BIT = 0x10  # the top bit of the last 5-bit group says the number is negative
# 13 groups of 5 bits hold any 64-bit number, and COCO tools never write a longer one.
_MAX_CHARACTERS_PER_NUMBER = 13


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
        number = count - counts[i - 2] if i > 2 else count
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


def counts_from_string(text: str, max_runs: int | None = None) -> list[int]:
    """Read run lengths from the COCO compressed form (``rleFrString``). Raises RLEError.

    With ``max_runs``, reading stops with an error as soon as the string holds more runs
    than that (or is too long to be worth reading), so a huge string costs no memory.
    """
    if max_runs is not None and len(text) > max_runs * _MAX_CHARACTERS_PER_NUMBER:
        raise RLEError(
            [
                f"'counts' is {len(text)} characters long, which is longer than the counts "
                f"of any mask with at most {max_runs - 1} pixels can be"
            ]
        )
    counts: list[int] = []
    position = 0
    while position < len(text):
        number = 0
        shift = 0
        start = position
        while True:
            if position >= len(text):
                raise RLEError(["'counts' ends in the middle of a number"])
            if position - start >= _MAX_CHARACTERS_PER_NUMBER:
                raise RLEError(
                    [
                        f"'counts' has a number written with more than "
                        f"{_MAX_CHARACTERS_PER_NUMBER} characters (from position {start + 1}), "
                        "which compressed COCO counts never need"
                    ]
                )
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
        if max_runs is not None and len(counts) > max_runs:
            raise RLEError([_too_many_runs(max_runs)])
    return counts


def _too_many_runs(max_runs: int) -> str:
    return (
        f"'counts' lists more than {max_runs} runs, but a mask can have at most one more run "
        "than it has pixels"
    )


# --- Checking a mask ----------------------------------------------------------------------


def problems(rle: object, max_pixels: int = MAX_PIXELS) -> list[str]:
    """Everything wrong with ``rle`` as a mask, in plain language; empty if it is valid.

    The messages are phrases such as ``"'size' must be ..."``, meant to follow a lead-in like
    "Mask 'lung' is not a valid COCO RLE:". ``max_pixels`` caps the mask's height x width.
    """
    return _check(rle, max_pixels)[0]


def validate(rle: object, max_pixels: int = MAX_PIXELS) -> None:
    """Raise RLEError if ``rle`` is not a valid mask. The mask itself is not changed."""
    if found := problems(rle, max_pixels):
        raise RLEError(found)


def _check(
    rle: object, max_pixels: int = MAX_PIXELS
) -> tuple[list[str], tuple[int, int] | None, list[int] | None]:
    """Return ``(problems, size, run lengths)``; size or run lengths are None if unreadable.

    Nothing here costs more than the size of the input: the size is checked against the
    caps before the runs are read, and the runs are read only up to the number a mask of
    that size can have.
    """
    if not isinstance(rle, dict):
        kind = describe_kind(rle)
        return [f"it must be an object with 'size' and 'counts', not {kind}"], None, None
    found = [
        f"unexpected key {key!r} (only 'size' and 'counts' are allowed)"
        for key in rle
        if key not in ("size", "counts")
    ]
    size = counts = None
    if "size" not in rle:
        found.append("it has no 'size' (give [height, width])")
    elif not _is_size(rle["size"]):
        found.append(
            "'size' must be two whole numbers [height, width], each at least 1, "
            f"not {rle['size']!r}"
        )
    elif too_big := _size_problem(rle["size"][0], rle["size"][1], max_pixels):
        found.append(too_big)
    else:
        size = (rle["size"][0], rle["size"][1])
    # A mask has at most one more run than pixels (when every pixel differs from the next
    # and the first is foreground); without a usable size, the cap stands in for the pixels.
    max_runs = (size[0] * size[1] if size else max_pixels) + 1
    if "counts" not in rle:
        found.append("it has no 'counts'")
    else:
        try:
            counts = _read_counts(rle["counts"], max_runs)
        except RLEError as error:
            found += error.problems
    if size and counts is not None and sum(counts) != size[0] * size[1]:
        found.append(
            f"'counts' add up to {sum(counts)} pixels, but a {size[0]} x {size[1]} mask "
            f"has {size[0] * size[1]}"
        )
    return found, size, counts


def _is_size(size: object) -> bool:
    return (
        isinstance(size, list | tuple)
        and len(size) == 2
        and all(_is_whole_number(side) and side >= 1 for side in size)
    )


def _size_problem(height: int, width: int, max_pixels: int) -> str | None:
    """Why a mask of this size is too big to handle, or None if it is within the caps."""
    if height > MAX_SIDE or width > MAX_SIDE:
        return (
            f"'size' is {height} x {width}, but a mask can be at most {MAX_SIDE} pixels "
            "high and wide"
        )
    if height * width > max_pixels:
        about = " (about 8192 x 8192)" if max_pixels == MAX_PIXELS else ""
        return (
            f"'size' is {height} x {width}, which is {height * width} pixels, but a mask "
            f"can have at most {max_pixels} pixels{about}"
        )
    return None


def _read_counts(counts: object, max_runs: int) -> list[int]:
    """The run lengths from either form of ``counts``, or an RLEError saying what is wrong."""
    if isinstance(counts, str):
        return counts_from_string(counts, max_runs)
    if isinstance(counts, list | tuple):
        if len(counts) > max_runs:
            raise RLEError([_too_many_runs(max_runs)])
        if all(_is_whole_number(count) and count >= 0 for count in counts):
            return list(counts)
        raise RLEError(["'counts' must contain only whole numbers that are 0 or more"])
    kind = describe_kind(counts)
    raise RLEError([f"'counts' must be a list of run lengths or a compressed string, not {kind}"])


def _is_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse(rle: object, max_pixels: int = MAX_PIXELS) -> tuple[tuple[int, int], list[int]]:
    """Return the size and the run lengths of a valid mask, or raise RLEError."""
    found, size, counts = _check(rle, max_pixels)
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


def decode(rle: RLE, max_pixels: int = MAX_PIXELS) -> np.ndarray:
    """Decode a COCO RLE (either form) to a boolean array of shape (height, width).

    This is the one operation that needs memory for every pixel, so the mask must be
    within ``max_pixels`` (and ``MAX_SIDE`` on each side); a bigger one raises RLEError.
    """
    (height, width), counts = _parse(rle, max_pixels)
    is_foreground = np.arange(len(counts)) % 2 == 1  # runs alternate, starting with background
    pixels = np.repeat(is_foreground, counts)
    return pixels.reshape((height, width), order="F")


def area(rle: RLE, max_pixels: int = MAX_PIXELS) -> int:
    """The number of foreground pixels: the sum of every second run (``rleArea``)."""
    _, counts = _parse(rle, max_pixels)
    return sum(counts[1::2])


def to_uncompressed(rle: RLE, max_pixels: int = MAX_PIXELS) -> RLE:
    """The same mask with ``counts`` as a list of run lengths. Returns a new dict."""
    (height, width), counts = _parse(rle, max_pixels)
    return {"size": [height, width], "counts": counts}


def to_compressed(rle: RLE, max_pixels: int = MAX_PIXELS) -> RLE:
    """The same mask with ``counts`` as the COCO compressed string. Returns a new dict."""
    (height, width), counts = _parse(rle, max_pixels)
    return {"size": [height, width], "counts": counts_to_string(counts)}


# --- Intersection over union --------------------------------------------------------------


def iou(first: RLE, second: RLE, max_pixels: int = MAX_PIXELS) -> float:
    """Intersection over union of two masks of the same size, straight from their runs.

    Neither mask is decoded. Two empty masks have an IoU of 1.0 (they agree). An empty
    mask and a non-empty one have 0. Raises RLEError for an invalid mask and ValueError
    for masks of different sizes.
    """
    first_size, first_counts = _parse(first, max_pixels)
    second_size, second_counts = _parse(second, max_pixels)
    if first_size != second_size:
        raise ValueError("Masks of different sizes cannot be compared.")
    return counts_iou(first_counts, second_counts)


def counts_iou(first: Sequence[int], second: Sequence[int]) -> float:
    """Intersection over union of two masks given as run lengths over the same pixels.

    The work grows with the number of runs, not the number of pixels, and nothing is
    decoded: the intersection is the sum, over each foreground run of the first mask, of
    how many foreground pixels of the second mask fall inside it, which a prefix sum over
    the second mask's runs answers with one binary search per run end. The union follows
    from the two areas. The result is exactly what ``mask_iou`` gives on the decoded masks.
    """
    first_edges = _run_edges(first)
    second_edges = _run_edges(second)
    if first_edges[-1] != second_edges[-1]:
        raise ValueError("Masks of different sizes cannot be compared.")
    first_area = int((first_edges[2::2] - first_edges[1:-1:2]).sum())  # the odd runs
    covered = _foreground_before(second_edges)
    intersection = int((covered(first_edges[2::2]) - covered(first_edges[1:-1:2])).sum())
    second_area = int(covered(second_edges[-1:])[0])
    union = first_area + second_area - intersection
    if union == 0:
        return 1.0
    return intersection / union


def _run_edges(counts: Sequence[int]) -> np.ndarray:
    """Where each run starts: ``edges[i]`` to ``edges[i + 1]`` is run ``i`` (odd runs are on)."""
    return np.concatenate(([0], np.cumsum(np.asarray(counts, dtype=np.int64))))


def _foreground_before(edges: np.ndarray):
    """A function giving, for sorted positions, the mask's foreground pixels before each."""
    lengths = np.diff(edges)
    lengths[::2] = 0  # only the odd runs are foreground
    before_run = np.concatenate(([0], np.cumsum(lengths)))  # foreground pixels before run i

    def covered(positions: np.ndarray) -> np.ndarray:
        # The run holding a position is the last one starting at or before it; searching
        # from the right skips any empty runs that start there too. Inside a foreground
        # run, the pixels from its start up to the position count as well.
        runs = np.searchsorted(edges, positions, side="right") - 1
        inside = np.where(runs % 2 == 1, positions - edges[runs], 0)
        return before_run[runs] + inside

    return covered


def mask_iou(first: np.ndarray, second: np.ndarray) -> float:
    """Intersection over union of two boolean masks of the same shape (the pixel reference).

    Two empty masks have an IoU of 1.0 (they agree). An empty mask and a non-empty one have 0.
    """
    if first.shape != second.shape:
        raise ValueError("Masks of different sizes cannot be compared.")
    union = int(np.count_nonzero(first | second))
    if union == 0:
        return 1.0
    return int(np.count_nonzero(first & second)) / union
