"""COCO RLE: hand-checked encodings, error messages, and Hypothesis round trips."""

import json

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from labelflow.labeltask import rle
from labelflow.labeltask.rle import RLEError

masks = arrays(dtype=bool, shape=st.tuples(st.integers(1, 12), st.integers(1, 12)))


def mask_from_rows(*rows: str) -> np.ndarray:
    """Draw a mask: '#' is foreground, '.' is background."""
    return np.array([[char == "#" for char in row] for row in rows])


# --- Hand-checked encodings ---------------------------------------------------------------
#
# Each compressed string below was worked out by hand from the rules in maskApi.c:
# one character per 5 bits (code 48 + bits, plus 32 when more follow), and from the fourth
# number on, each number is stored as the difference from the one two places before it.

KNOWN_ENCODINGS = [
    # all foreground: a first run of 0 background pixels, then 4 foreground pixels
    pytest.param(["##", "##"], [0, 4], "04", id="all-foreground"),
    # all background is a single run
    pytest.param(["...", "...", "..."], [9], "9", id="all-background"),
    # columns are read top to bottom: (1,1,0) then (0,1,0), so the runs are 0, 2, 2, 1, 1;
    # the 4th and 5th numbers are stored as 1 - 2 = -1, which is the character 'O'
    pytest.param(["#.", "##", ".."], [0, 2, 2, 1, 1], "022OO", id="column-major"),
    # a column of 0,1,0,1: the 3rd run (1) is stored as it is, only the 4th (1 - 1 = 0) is a
    # difference; if the 3rd were a difference too, the string would be "1100"
    pytest.param([".", "#", ".", "#"], [1, 1, 1, 1], "1110", id="difference-starts-at-fourth"),
    # 16 does not fit in 5 bits: '`' (16 + 32 + 48) says more follows, then '0' (0 + 48)
    pytest.param(["." * 4] * 4, [16], "`0", id="two-characters"),
]


@pytest.mark.parametrize(("rows", "counts", "text"), KNOWN_ENCODINGS)
def test_known_encodings(rows, counts, text):
    mask = mask_from_rows(*rows)
    height, width = mask.shape
    assert rle.encode(mask, compressed=False) == {"size": [height, width], "counts": counts}
    assert rle.encode(mask) == {"size": [height, width], "counts": text}
    assert rle.decode({"size": [height, width], "counts": text}).tolist() == mask.tolist()


def test_matches_what_pycocotools_writes():
    """The string below is the output of pycocotools 2.0.11 (``mask.encode``) for this mask."""
    mask = np.zeros((12, 40), dtype=bool)
    mask[2:9, 5:30] = True  # a rectangle
    mask[0, 39] = True  # and a single pixel in each of two corners
    mask[11, 0] = True
    from_pycocotools = {"size": [12, 40], "counts": ";1b16cN" + "0" * 47 + "Z3JlL"}
    assert rle.encode(mask) == from_pycocotools
    assert np.array_equal(rle.decode(from_pycocotools), mask)
    assert rle.to_uncompressed(from_pycocotools)["counts"][:6] == [11, 1, 50, 7, 5, 7]


def test_negative_differences_over_several_characters():
    # 3 - 20 = -17 is written as '_' then 'O'; 8 - 5 = 3 is '3'
    counts = [0, 20, 5, 3, 8]
    assert rle.counts_to_string(counts) == "0d05_O3"
    assert rle.counts_from_string("0d05_O3") == counts
    assert rle.area({"size": [6, 6], "counts": counts}) == 23


def test_encoding_is_json_compatible():
    encoded = rle.encode(mask_from_rows("#.", ".#"))
    assert json.loads(json.dumps(encoded)) == encoded
    assert all(type(side) is int for side in encoded["size"])
    uncompressed = rle.encode(mask_from_rows("#.", ".#"), compressed=False)
    assert all(type(run) is int for run in uncompressed["counts"])


def test_any_nonzero_value_is_foreground():
    # down the columns the pixels are: background, foreground, foreground, background
    assert rle.encode(np.array([[0, 7], [1, 0]]), compressed=False)["counts"] == [1, 2, 1]


@pytest.mark.parametrize("bad", [np.zeros(4, dtype=bool), np.zeros((0, 3), dtype=bool)])
def test_encode_needs_a_2d_mask_with_pixels(bad):
    with pytest.raises(ValueError, match="2-D array with at least one pixel"):
        rle.encode(bad)


# --- Decoding, area, conversion -----------------------------------------------------------


def test_decode_gives_height_by_width_booleans():
    decoded = rle.decode({"size": [3, 2], "counts": [0, 2, 2, 1, 1]})
    assert decoded.dtype == bool
    assert decoded.shape == (3, 2)
    # Zero-length runs in the middle are unusual but they decode to the one obvious mask.
    assert rle.decode({"size": [2, 2], "counts": [0, 0, 2, 2]}).tolist() == [
        [False, True],
        [False, True],
    ]


def test_area_counts_foreground_pixels():
    assert rle.area({"size": [3, 2], "counts": [0, 2, 2, 1, 1]}) == 3
    assert rle.area({"size": [3, 2], "counts": "022OO"}) == 3
    assert rle.area({"size": [2, 2], "counts": [4]}) == 0


def test_conversion_between_the_two_forms_gives_new_dicts():
    uncompressed = {"size": [3, 2], "counts": [0, 2, 2, 1, 1]}
    compressed = {"size": [3, 2], "counts": "022OO"}
    assert rle.to_compressed(uncompressed) == compressed
    assert rle.to_uncompressed(compressed) == uncompressed
    assert uncompressed == {"size": [3, 2], "counts": [0, 2, 2, 1, 1]}  # the input is untouched
    # A mask already in the requested form comes back as an equal, separate dict.
    again = rle.to_uncompressed(uncompressed)
    assert again == uncompressed
    assert again is not uncompressed
    assert again["counts"] is not uncompressed["counts"]


# --- Hypothesis round trips ---------------------------------------------------------------


@given(masks, st.booleans())
def test_decode_undoes_encode(mask, compressed):
    encoded = rle.encode(mask, compressed=compressed)
    assert encoded["size"] == list(mask.shape)
    assert np.array_equal(rle.decode(encoded), mask)


@given(masks)
def test_the_runs_describe_the_mask_column_by_column(mask):
    counts = rle.encode(mask, compressed=False)["counts"]
    assert sum(counts) == mask.size
    assert all(run > 0 for run in counts[1:])  # only the first run may be empty
    assert (counts[0] == 0) == bool(mask[0, 0])
    # Reading the mask column by column gives the same pixels as the runs describe.
    pixels = np.repeat(np.arange(len(counts)) % 2 == 1, counts)
    assert np.array_equal(pixels, mask.T.ravel())
    assert rle.area(rle.encode(mask)) == rle.area(rle.encode(mask, compressed=False)) == mask.sum()


@given(masks)
def test_both_forms_describe_the_same_mask(mask):
    uncompressed = rle.encode(mask, compressed=False)
    compressed = rle.encode(mask, compressed=True)
    assert rle.to_compressed(uncompressed) == compressed
    assert rle.to_uncompressed(compressed) == uncompressed
    assert rle.to_uncompressed(rle.to_compressed(uncompressed)) == uncompressed


@given(st.lists(st.integers(min_value=0, max_value=2**31), min_size=1, max_size=40))
def test_counts_survive_the_string_form(counts):
    assert rle.counts_from_string(rle.counts_to_string(counts)) == counts


# --- IoU ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "iou"),
    [
        pytest.param(["###.", "...."], [".###", "...."], 2 / 4, id="overlapping"),
        pytest.param(["...", "..."], ["...", "..."], 1.0, id="two-empty-masks-agree"),
        pytest.param(["..."], ["#.."], 0.0, id="empty-against-non-empty"),
    ],
)
def test_iou(first, second, iou):
    assert rle.mask_iou(mask_from_rows(*first), mask_from_rows(*second)) == iou


def test_iou_needs_masks_of_the_same_size():
    with pytest.raises(ValueError, match="different sizes"):
        rle.mask_iou(mask_from_rows("..."), mask_from_rows("..", ".."))


# --- Bad masks, in plain language ---------------------------------------------------------

BAD_MASKS = [
    pytest.param(None, "it must be an object with 'size' and 'counts', not null", id="null"),
    pytest.param([], "it must be an object with 'size' and 'counts', not a list", id="list"),
    pytest.param({"counts": [4]}, "it has no 'size'", id="no-size"),
    pytest.param({"size": [2, 2]}, "it has no 'counts'", id="no-counts"),
    pytest.param(
        {"size": [2], "counts": [2]},
        "'size' must be two whole numbers [height, width], each at least 1, not [2]",
        id="short-size",
    ),
    pytest.param(
        {"size": [0, 2], "counts": [0]},
        "'size' must be two whole numbers [height, width], each at least 1, not [0, 2]",
        id="zero-size",
    ),
    pytest.param(
        {"size": [2.0, 2], "counts": [4]},
        "'size' must be two whole numbers",
        id="fractional-size",
    ),
    pytest.param(
        {"size": [True, 2], "counts": [2]}, "'size' must be two whole numbers", id="bool-size"
    ),
    pytest.param(
        {"size": [2, 2], "counts": 4},
        "'counts' must be a list of run lengths or a compressed string, not a number",
        id="number-counts",
    ),
    pytest.param(
        {"size": [2, 2], "counts": [2, -2, 4]},
        "'counts' must contain only whole numbers that are 0 or more",
        id="negative-run",
    ),
    pytest.param(
        {"size": [2, 2], "counts": [2.5, 1.5]},
        "'counts' must contain only whole numbers that are 0 or more",
        id="fractional-run",
    ),
    pytest.param(
        {"size": [2, 2], "counts": [1, 2]},
        "'counts' add up to 3 pixels, but a 2 x 2 mask has 4",
        id="runs-too-short",
    ),
    pytest.param(
        {"size": [2, 2], "counts": [0, 9]},
        "'counts' add up to 9 pixels, but a 2 x 2 mask has 4",
        id="runs-too-long",
    ),
    pytest.param(
        {"size": [2, 2], "counts": "0~"},
        "'counts' contains the character '~' (position 2), which compressed COCO counts never use",
        id="bad-character",
    ),
    pytest.param(
        {"size": [2, 2], "counts": "0d"},
        "'counts' ends in the middle of a number",
        id="cut-off-string",
    ),
    pytest.param(
        {"size": [2, 2], "counts": "O"},
        "'counts' has a negative run length at entry 1",
        id="negative-string-run",
    ),
    pytest.param(
        {"size": [2, 2], "counts": [4], "extra": 1},
        "unexpected key 'extra' (only 'size' and 'counts' are allowed)",
        id="extra-key",
    ),
]


@pytest.mark.parametrize(("bad", "message"), BAD_MASKS)
def test_a_bad_mask_is_described_plainly_and_refused_everywhere(bad, message):
    assert any(message in problem for problem in rle.problems(bad))
    with pytest.raises(RLEError, match="Not a valid COCO RLE") as info:
        rle.validate(bad)
    assert any(message in problem for problem in info.value.problems)
    for use in (rle.decode, rle.area, rle.to_compressed, rle.to_uncompressed):
        with pytest.raises(RLEError):
            use(bad)


def test_a_mask_can_have_several_problems_at_once():
    found = rle.problems({"size": [0, 2], "counts": "~", "extra": 1})
    assert len(found) == 3


def test_a_good_mask_has_no_problems():
    assert rle.problems({"size": [3, 2], "counts": "022OO"}) == []
    assert rle.problems({"size": [3, 2], "counts": [0, 2, 2, 1, 1]}) == []
    rle.validate({"size": [3, 2], "counts": (0, 2, 2, 1, 1)})  # a tuple is a list too
