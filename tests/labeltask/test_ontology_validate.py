"""Validating submitted labels: what passes unchanged, and every plain-language rejection."""

import copy

import numpy as np
import pytest

from ltq.labeltask import LabelValidationError, load_ontology, rle

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
    checked = task.validate(label)
    assert checked == label
    assert checked is not label
    assert checked["masks"] is not label["masks"]
    assert checked["masks"]["lung"] is not label["masks"]["lung"]
    checked["classifications"]["view"] = "AP"
    assert label["classifications"]["view"] == "PA"


def test_a_label_is_not_re_encoded_or_tidied(task, block):
    """Individual labels are stored exactly as submitted, in whichever count form."""
    odd_but_valid = {"size": [10, 10], "counts": [0, 0, 50, 50]}  # not what encode() would write
    label = {
        "classifications": {"view": "PA", "quality": None},
        "masks": {
            "lung": odd_but_valid,
            "effusion": rle.encode(block(0), compressed=False),
        },
    }
    checked = task.validate(label)
    assert checked == label
    assert checked["masks"]["lung"]["counts"] == [0, 0, 50, 50]
    assert checked["masks"]["effusion"]["counts"] == [100]
    assert checked["classifications"] == {"view": "PA", "quality": None}


def test_both_count_forms_are_accepted(task, block, make_label):
    compressed = make_label(lung=rle.encode(block(30), compressed=True))
    uncompressed = make_label(lung=rle.encode(block(30), compressed=False))
    assert isinstance(compressed["masks"]["lung"]["counts"], str)
    assert isinstance(uncompressed["masks"]["lung"]["counts"], list)
    assert task.validate(compressed) == compressed
    assert task.validate(uncompressed) == uncompressed


def test_an_optional_classification_may_be_absent_or_null(task, make_label):
    absent = make_label(quality=None)
    assert "quality" not in absent["classifications"]
    assert task.validate(absent) == absent
    null = make_label()
    null["classifications"]["quality"] = None
    assert task.validate(null) == null


def test_an_empty_mask_is_valid(task, make_label, block):
    label = make_label(lung=block(0), effusion=block(0))
    assert rle.area(label["masks"]["lung"]) == 0
    assert task.validate(label) == label


def test_validation_does_not_change_the_submitted_label(task, make_label):
    label = make_label()
    snapshot = copy.deepcopy(label)
    task.validate(label)
    assert label == snapshot


def test_a_tuple_size_is_accepted(task, make_label):
    label = make_label()
    label["masks"]["lung"]["size"] = (10, 10)
    assert task.validate(label) == label


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
    found = problems_of(task, label)
    assert found[0] == (
        "Unknown key 'clasifications' in the label. Did you mean 'classifications'? "
        "A label has only 'classifications', 'masks'."
    )
    # the classifications it carried are then missing as well
    assert (
        "Required classification 'view' is missing. Allowed values: 'PA', 'AP', 'lateral'." in found
    )


def test_unknown_top_level_key_with_no_close_match(task, make_label):
    label = make_label()
    label["notes"] = "hello"
    assert problems_of(task, label) == [
        "Unknown key 'notes' in the label. A label has only 'classifications', 'masks'."
    ]


def test_unknown_classification_suggests_the_closest_name(task, make_label):
    label = make_label()
    label["classifications"]["veiw"] = "PA"
    assert problems_of(task, label) == ["Unknown classification 'veiw'. Did you mean 'view'?"]


def test_unknown_classification_with_no_close_match_lists_the_known_ones(task, make_label):
    label = make_label()
    label["classifications"]["laterality"] = "left"
    assert problems_of(task, label) == [
        "Unknown classification 'laterality'. "
        "The classifications in this ontology are: 'view', 'quality'."
    ]


def test_unknown_mask_class_suggests_the_closest_name(task, make_label):
    label = make_label()
    label["masks"]["lungs"] = label["masks"]["lung"]
    assert problems_of(task, label) == ["Unknown mask class 'lungs'. Did you mean 'lung'?"]


def test_unknown_mask_class_with_no_close_match_lists_the_known_ones(task, make_label):
    label = make_label()
    label["masks"]["heart"] = label["masks"]["lung"]
    assert problems_of(task, label) == [
        "Unknown mask class 'heart'. The mask classes in this ontology are: 'lung', 'effusion'."
    ]


def test_a_mask_class_given_as_a_classification_is_pointed_to_masks(task, make_label):
    label = make_label()
    label["classifications"]["lung"] = "yes"
    assert problems_of(task, label) == [
        "'lung' is a mask class, not a classification. Put it under 'masks'."
    ]


def test_a_classification_given_as_a_mask_is_pointed_to_classifications(task, make_label):
    label = make_label()
    label["masks"]["view"] = label["masks"]["lung"]
    assert problems_of(task, label) == [
        "'view' is a classification, not a mask class. Put it under 'classifications'."
    ]


def test_a_value_that_is_not_allowed_names_the_allowed_values(task, make_label):
    label = make_label(view="sideways")
    assert problems_of(task, label) == [
        "Classification 'view' has the value 'sideways', which is not allowed. "
        "Allowed values: 'PA', 'AP', 'lateral'."
    ]


def test_a_close_value_gets_a_suggestion(task, make_label):
    label = make_label(view="lateral ")
    assert problems_of(task, label) == [
        "Classification 'view' has the value 'lateral ', which is not allowed. "
        "Did you mean 'lateral'? Allowed values: 'PA', 'AP', 'lateral'."
    ]


@pytest.mark.parametrize("value", [3, True, ["PA"], {"a": 1}])
def test_a_value_of_the_wrong_kind_is_not_allowed(task, make_label, value):
    label = make_label(view=value)
    assert problems_of(task, label) == [
        f"Classification 'view' has the value {value!r}, which is not allowed. "
        "Allowed values: 'PA', 'AP', 'lateral'."
    ]


def test_an_optional_classification_with_a_bad_value_is_rejected(task, make_label):
    label = make_label(quality="excellent")
    assert problems_of(task, label) == [
        "Classification 'quality' has the value 'excellent', which is not allowed. "
        "Allowed values: 'good', 'poor'."
    ]


def test_a_missing_required_classification(task, make_label):
    label = make_label()
    del label["classifications"]["view"]
    assert problems_of(task, label) == [
        "Required classification 'view' is missing. Allowed values: 'PA', 'AP', 'lateral'."
    ]


def test_a_null_required_classification_counts_as_missing(task, make_label):
    label = make_label(view=None)
    label["classifications"]["view"] = None
    assert problems_of(task, label) == [
        "Required classification 'view' is missing. Allowed values: 'PA', 'AP', 'lateral'."
    ]


def test_a_label_with_no_classifications_part_misses_the_required_ones(task, make_label):
    label = make_label()
    del label["classifications"]
    assert problems_of(task, label) == [
        "Required classification 'view' is missing. Allowed values: 'PA', 'AP', 'lateral'."
    ]


def test_a_missing_mask_class(task, make_label):
    label = make_label()
    del label["masks"]["effusion"]
    assert problems_of(task, label) == [f"The mask for 'effusion' is missing. {EMPTY_MASK_HINT}"]


def test_a_label_with_no_masks_part_misses_every_mask_class(task, make_label):
    label = make_label()
    del label["masks"]
    assert problems_of(task, label) == [
        f"The mask for 'lung' is missing. {EMPTY_MASK_HINT}",
        f"The mask for 'effusion' is missing. {EMPTY_MASK_HINT}",
    ]


def test_a_null_mask_is_malformed_not_missing(task, make_label):
    label = make_label()
    label["masks"]["lung"] = None
    assert problems_of(task, label) == [
        "Mask 'lung' is not a valid COCO RLE: it must be an object with 'size' and 'counts', "
        "not null."
    ]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (
            {"counts": [100]},
            "Mask 'lung' is not a valid COCO RLE: it has no 'size' (give [height, width]).",
        ),
        (
            {"size": [10, 10]},
            "Mask 'lung' is not a valid COCO RLE: it has no 'counts'.",
        ),
        (
            {"size": [10], "counts": [100]},
            "Mask 'lung' is not a valid COCO RLE: 'size' must be two whole numbers "
            "[height, width], each at least 1, not [10].",
        ),
        (
            {"size": [10, 10], "counts": "0~"},
            "Mask 'lung' is not a valid COCO RLE: 'counts' contains the character '~' "
            "(position 2), which compressed COCO counts never use.",
        ),
        (
            {"size": [10, 10], "counts": [0, -5, 105]},
            "Mask 'lung' is not a valid COCO RLE: 'counts' must contain only whole numbers "
            "that are 0 or more.",
        ),
        (
            {"size": [10, 10], "counts": {"0": 100}},
            "Mask 'lung' is not a valid COCO RLE: 'counts' must be a list of run lengths or a "
            "compressed string, not an object.",
        ),
        (
            {"size": [10, 10], "counts": [0, 99]},
            "Mask 'lung' is not a valid COCO RLE: 'counts' add up to 99 pixels, but a "
            "10 x 10 mask has 100.",
        ),
        (
            {"size": [10, 10], "counts": [0, 101]},
            "Mask 'lung' is not a valid COCO RLE: 'counts' add up to 101 pixels, but a "
            "10 x 10 mask has 100.",
        ),
        (
            {"size": [10, 10], "counts": [100], "area": 0},
            "Mask 'lung' is not a valid COCO RLE: unexpected key 'area' (only 'size' and "
            "'counts' are allowed).",
        ),
    ],
)
def test_a_malformed_mask(task, make_label, bad, message):
    label = make_label(lung=bad)
    assert problems_of(task, label) == [message]


def test_counts_that_do_not_fill_the_image(task, make_label):
    # a 512 x 512 mask whose runs add up to a 256 x 256 image
    label = make_label(lung={"size": [512, 512], "counts": [0, 65536]})
    assert problems_of(task, label) == [
        "Mask 'lung' is not a valid COCO RLE: 'counts' add up to 65536 pixels, but a "
        "512 x 512 mask has 262144."
    ]


def test_masks_of_different_sizes_in_one_label(task, make_label, block):
    label = make_label(lung=block(40), effusion=block(0, 5, 5))
    assert problems_of(task, label) == [
        "The masks are not all the same size: 'lung' is 10 x 10, 'effusion' is 5 x 5. "
        "Every mask in a label must have the same size."
    ]


def test_masks_of_the_same_size_are_not_flagged(task, make_label, block):
    label = make_label(lung=block(40, 4, 25), effusion=block(0, 4, 25))
    assert task.validate(label) == label


def test_a_malformed_mask_is_not_also_reported_as_a_size_mismatch(task, make_label, block):
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
