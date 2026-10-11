"""Validating submitted labels: what passes unchanged, and every plain-language rejection."""

import copy

import numpy as np
import pytest

from labelflow.labeltask import LabelValidationError, load_ontology, rle

ALLOWED_VIEWS = "Allowed values: 'PA', 'AP', 'lateral'."
VIEW_IS_MISSING = f"Required classification 'view' is missing. {ALLOWED_VIEWS}"
EMPTY_MASK_HINT = (
    "Every mask class must be present; for an empty mask, give an RLE with no foreground, "
    'such as {"size": [512, 512], "counts": [262144]} for a 512 x 512 image.'
)


def problems_of(task, label) -> list[str]:
    with pytest.raises(LabelValidationError) as info:
        task.validate(label)
    return info.value.problems


# --- Valid labels -------------------------------------------------------------------------


def test_a_valid_label_comes_back_equal_but_separate(task, make_label):
    label = make_label()
    snapshot = copy.deepcopy(label)
    checked = task.validate(label)
    assert checked == label == snapshot
    assert checked is not label
    assert checked["masks"] is not label["masks"]
    assert checked["masks"]["lung"] is not label["masks"]["lung"]
    checked["classifications"]["view"] = "AP"
    assert label["classifications"]["view"] == "PA"


@pytest.mark.parametrize(
    "variation",
    [
        pytest.param(lambda block: {"quality": None}, id="optional-classification-left-out"),
        pytest.param(lambda block: {"lung": rle.encode(block(30))}, id="compressed-counts"),
        pytest.param(
            lambda block: {"lung": rle.encode(block(30), compressed=False)},
            id="uncompressed-counts",
        ),
        pytest.param(lambda block: {"lung": block(0), "effusion": block(0)}, id="empty-masks"),
        pytest.param(
            lambda block: {"lung": block(40, 4, 25), "effusion": block(0, 4, 25)},
            id="masks-of-the-same-non-square-size",
        ),
        pytest.param(lambda block: {"lung": {"size": (10, 10), "counts": [100]}}, id="tuple-size"),
        # not what encode() would write, but valid: individual labels are stored as submitted
        pytest.param(
            lambda block: {"lung": {"size": [10, 10], "counts": [0, 0, 50, 50]}},
            id="non-canonical-runs",
        ),
    ],
)
def test_valid_variations_are_accepted_as_they_are(task, make_label, block, variation):
    label = make_label(**variation(block))
    assert task.validate(label) == label


def test_an_optional_classification_may_be_null(task, make_label):
    label = make_label()
    label["classifications"]["quality"] = None
    assert task.validate(label) == label
    assert task.validate(label)["classifications"] == {"view": "PA", "quality": None}


# --- Rejections, in plain language --------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "kind"), [(None, "null"), ([], "a list"), ("PA", "text"), (3, "a number")]
)
def test_a_label_must_be_an_object(task, label, kind):
    assert problems_of(task, label) == [
        f"A label must be an object with 'classifications' and 'masks', not {kind}."
    ]


def test_unknown_top_level_key(task, make_label):
    label = make_label()
    label["clasifications"] = label.pop("classifications")
    assert problems_of(task, label) == [
        "Unknown key 'clasifications' in the label. Did you mean 'classifications'? "
        "A label has only 'classifications', 'masks'.",
        VIEW_IS_MISSING,  # the classifications it carried are then missing as well
    ]
    label = make_label()
    label["notes"] = "hello"
    assert problems_of(task, label) == [
        "Unknown key 'notes' in the label. A label has only 'classifications', 'masks'."
    ]


@pytest.mark.parametrize(
    ("section", "name", "message"),
    [
        ("classifications", "veiw", "Unknown classification 'veiw'. Did you mean 'view'?"),
        (
            "classifications",
            "laterality",
            "Unknown classification 'laterality'. "
            "The classifications in this ontology are: 'view', 'quality'.",
        ),
        (
            "classifications",
            "lung",
            "'lung' is a mask class, not a classification. Put it under 'masks'.",
        ),
        ("masks", "lungs", "Unknown mask class 'lungs'. Did you mean 'lung'?"),
        (
            "masks",
            "heart",
            "Unknown mask class 'heart'. "
            "The mask classes in this ontology are: 'lung', 'effusion'.",
        ),
        (
            "masks",
            "view",
            "'view' is a classification, not a mask class. Put it under 'classifications'.",
        ),
    ],
)
def test_an_unknown_name_gets_a_suggestion_or_the_known_names(
    task, make_label, section, name, message
):
    label = make_label()
    label[section][name] = "PA" if section == "classifications" else label["masks"]["lung"]
    assert problems_of(task, label) == [message]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param(
            "view",
            "sideways",
            "Classification 'view' has the value 'sideways', which is not allowed. "
            f"{ALLOWED_VIEWS}",
            id="not-allowed",
        ),
        pytest.param(
            "view",
            "lateral ",
            "Classification 'view' has the value 'lateral ', which is not allowed. "
            f"Did you mean 'lateral'? {ALLOWED_VIEWS}",
            id="close-to-an-allowed-value",
        ),
        pytest.param(
            "quality",
            "excellent",
            "Classification 'quality' has the value 'excellent', which is not allowed. "
            "Allowed values: 'good', 'poor'.",
            id="optional-classification",
        ),
        *(
            pytest.param(
                "view",
                value,
                f"Classification 'view' has the value {value!r}, which is not allowed. "
                f"{ALLOWED_VIEWS}",
                id=f"wrong-kind-{type(value).__name__}",
            )
            for value in (3, True, ["PA"], {"a": 1})
        ),
    ],
)
def test_a_value_that_is_not_allowed_names_the_allowed_values(
    task, make_label, field, value, message
):
    assert problems_of(task, make_label(**{field: value})) == [message]


@pytest.mark.parametrize(
    "remove_the_view",
    [
        pytest.param(lambda label: label["classifications"].pop("view"), id="absent"),
        pytest.param(lambda label: label["classifications"].update(view=None), id="null"),
        pytest.param(lambda label: label.pop("classifications"), id="no-classifications-part"),
    ],
)
def test_a_missing_required_classification(task, make_label, remove_the_view):
    label = make_label()
    remove_the_view(label)
    assert problems_of(task, label) == [VIEW_IS_MISSING]


def test_a_missing_mask_class(task, make_label):
    label = make_label()
    del label["masks"]["effusion"]
    assert problems_of(task, label) == [f"The mask for 'effusion' is missing. {EMPTY_MASK_HINT}"]
    del label["masks"]
    assert problems_of(task, label) == [
        f"The mask for 'lung' is missing. {EMPTY_MASK_HINT}",
        f"The mask for 'effusion' is missing. {EMPTY_MASK_HINT}",
    ]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        pytest.param(None, "it must be an object with 'size' and 'counts', not null.", id="null"),
        pytest.param({"counts": [100]}, "it has no 'size' (give [height, width]).", id="no-size"),
        pytest.param({"size": [10, 10]}, "it has no 'counts'.", id="no-counts"),
        pytest.param(
            {"size": [10], "counts": [100]},
            "'size' must be two whole numbers [height, width], each at least 1, not [10].",
            id="short-size",
        ),
        pytest.param(
            {"size": [10, 10], "counts": "0~"},
            "'counts' contains the character '~' (position 2), which compressed COCO counts "
            "never use.",
            id="bad-character",
        ),
        pytest.param(
            {"size": [10, 10], "counts": [0, -5, 105]},
            "'counts' must contain only whole numbers that are 0 or more.",
            id="negative-run",
        ),
        pytest.param(
            {"size": [10, 10], "counts": {"0": 100}},
            "'counts' must be a list of run lengths or a compressed string, not an object.",
            id="counts-is-an-object",
        ),
        pytest.param(
            {"size": [10, 10], "counts": [0, 99]},
            "'counts' add up to 99 pixels, but a 10 x 10 mask has 100.",
            id="runs-too-short",
        ),
        pytest.param(
            {"size": [10, 10], "counts": [0, 101]},
            "'counts' add up to 101 pixels, but a 10 x 10 mask has 100.",
            id="runs-too-long",
        ),
        # a 512 x 512 mask whose runs add up to a 256 x 256 image
        pytest.param(
            {"size": [512, 512], "counts": [0, 65536]},
            "'counts' add up to 65536 pixels, but a 512 x 512 mask has 262144.",
            id="runs-fill-a-smaller-image",
        ),
        pytest.param(
            {"size": [10, 10], "counts": [100], "area": 0},
            "unexpected key 'area' (only 'size' and 'counts' are allowed).",
            id="extra-key",
        ),
    ],
)
def test_a_malformed_mask_is_not_a_valid_coco_rle(task, make_label, bad, message):
    label = make_label()
    label["masks"]["lung"] = bad
    assert problems_of(task, label) == [f"Mask 'lung' is not a valid COCO RLE: {message}"]


# --- Masks too big to handle --------------------------------------------------------------

HUGE = {"size": [100000, 100000], "counts": "PPigPZ9"}  # 10 gigapixels in seven characters


def test_a_mask_claiming_a_huge_image_is_refused_in_plain_language(task, make_label):
    label = make_label(lung=HUGE, effusion=HUGE)
    assert problems_of(task, label) == [
        "Mask 'lung' is not a valid COCO RLE: 'size' is 100000 x 100000, but a mask can be "
        "at most 16384 pixels high and wide.",
        "Mask 'effusion' is not a valid COCO RLE: 'size' is 100000 x 100000, but a mask can "
        "be at most 16384 pixels high and wide.",
    ]


def test_a_huge_mask_is_refused_as_a_key_too(task, make_label):
    with pytest.raises(LabelValidationError, match=r"The key is invalid.*at most 16384 pixels"):
        task.score(make_label(), make_label(lung=HUGE))


def test_a_mask_over_the_pixel_cap_names_the_cap(task, make_label):
    too_many = {"size": [8192, 8193], "counts": [8192 * 8193]}
    label = make_label(lung=too_many, effusion=too_many)
    assert problems_of(task, label) == [
        "Mask 'lung' is not a valid COCO RLE: 'size' is 8192 x 8193, which is 67117056 pixels, "
        "but a mask can have at most 67108864 pixels (about 8192 x 8192).",
        "Mask 'effusion' is not a valid COCO RLE: 'size' is 8192 x 8193, which is 67117056 "
        "pixels, but a mask can have at most 67108864 pixels (about 8192 x 8192).",
    ]


def test_a_giant_counts_string_is_refused(task, make_label):
    label = make_label(lung={"size": [10, 10], "counts": "0" * 5000})
    assert problems_of(task, label) == [
        "Mask 'lung' is not a valid COCO RLE: 'counts' is 5000 characters long, which is longer "
        "than the counts of any mask with at most 100 pixels can be."
    ]


def test_an_ontology_can_set_its_own_pixel_cap(make_label, block):
    task = load_ontology(
        "name: small\nversion: 1\nmax_mask_pixels: 50\nmasks:\n  - name: lung\n    threshold: 0.5\n"
    )
    fits = {"classifications": {}, "masks": {"lung": rle.encode(block(25, 5, 10))}}
    assert task.validate(fits) == fits
    too_big = {"classifications": {}, "masks": {"lung": rle.encode(block(0))}}  # 10 x 10
    assert problems_of(task, too_big) == [
        "Mask 'lung' is not a valid COCO RLE: 'size' is 10 x 10, which is 100 pixels, but a "
        "mask can have at most 50 pixels."
    ]
    # the consensus check honours the same cap, and never decodes a mask
    result = task.check_consensus([fits, fits])
    assert result.agreed and result.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 1.0]]


def test_masks_of_different_sizes_in_one_label(task, make_label, block):
    label = make_label(lung=block(40), effusion=block(0, 5, 5))
    assert problems_of(task, label) == [
        "The masks are not all the same size: 'lung' is 10 x 10, 'effusion' is 5 x 5. "
        "Every mask in a label must have the same size."
    ]
    # A malformed mask has no size to compare, so it is not also reported as a mismatch.
    label = make_label(lung={"size": [10, 10], "counts": [5]}, effusion=block(0, 5, 5))
    found = problems_of(task, label)
    assert len(found) == 1
    assert found[0].startswith("Mask 'lung' is not a valid COCO RLE:")


def test_the_two_parts_must_be_objects(task, make_label):
    label = make_label()
    label["classifications"] = ["view", "PA"]
    label["masks"] = "lung"
    assert problems_of(task, label) == [
        "'classifications' must be an object of name and value pairs, not a list.",
        "'masks' must be an object that gives an RLE for each mask class, not text.",
    ]


def test_every_problem_is_reported_at_once(task, make_label):
    label = {
        "colour": "red",
        "classifications": {"view": "PAA", "qualty": "good"},
        "masks": {"lung": {"size": [10, 10], "counts": [1]}, "heart": {}},
    }
    found = problems_of(task, label)
    assert len(found) == 6
    assert found[0].startswith("Unknown key 'colour'")
    assert any("Classification 'view' has the value 'PAA'" in problem for problem in found)
    assert any("Unknown classification 'qualty'. Did you mean 'quality'?" in p for p in found)
    assert any("Mask 'lung' is not a valid COCO RLE" in problem for problem in found)
    assert any("Unknown mask class 'heart'" in problem for problem in found)
    assert any("The mask for 'effusion' is missing" in problem for problem in found)


def test_the_error_message_joins_the_problems(task, make_label):
    label = make_label(view="sideways")
    del label["masks"]["effusion"]
    with pytest.raises(LabelValidationError) as info:
        task.validate(label)
    assert len(info.value.problems) == 2
    assert str(info.value) == "; ".join(info.value.problems)


# --- An ontology with only some kinds of fields -------------------------------------------


def test_an_ontology_with_no_masks_rejects_any_mask():
    only_classes = load_ontology(
        "name: only_classes\nversion: 1\nclassifications:\n  - name: view\n    values: [PA]\n"
    )
    assert only_classes.validate({"classifications": {"view": "PA"}}) == {
        "classifications": {"view": "PA"}
    }
    assert only_classes.validate({"classifications": {"view": "PA"}, "masks": {}})
    mask = rle.encode(np.zeros((2, 2), dtype=bool))
    assert problems_of(
        only_classes, {"classifications": {"view": "PA"}, "masks": {"lung": mask}}
    ) == ["Unknown mask class 'lung'. This ontology has no mask classes."]


def test_an_ontology_with_no_classifications_rejects_any_classification():
    only_masks = load_ontology(
        "name: only_masks\nversion: 1\nmasks:\n  - name: lung\n    threshold: 0.5\n"
    )
    mask = rle.encode(np.zeros((2, 2), dtype=bool))
    assert only_masks.validate({"masks": {"lung": mask}}) == {"masks": {"lung": mask}}
    assert problems_of(
        only_masks, {"classifications": {"view": "PA"}, "masks": {"lung": mask}}
    ) == ["Unknown classification 'view'. This ontology has no classifications."]
