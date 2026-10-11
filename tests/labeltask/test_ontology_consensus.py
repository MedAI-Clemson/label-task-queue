"""The consensus check and scoring against a key, with handmade masks."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from labelflow.labeltask import ConsensusResult, LabelValidationError, load_ontology, rle

# Lung masks agree at an IoU of 0.8 or more, effusion masks at 0.5 or more.
LUNG_THRESHOLD = 0.8
EFFUSION_THRESHOLD = 0.5

TASK = load_ontology(
    Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "chest_xray.yaml"
)


def assert_plain_json(value):
    """Fail unless the value is made only of dicts, lists, strings, numbers, booleans and null."""
    if isinstance(value, dict):
        assert all(isinstance(key, str) for key in value)
        for item in value.values():
            assert_plain_json(item)
    elif isinstance(value, list):
        for item in value:
            assert_plain_json(item)
    else:
        assert type(value) in (str, int, float, bool, type(None)), f"not plain JSON: {value!r}"


# --- Agreement ----------------------------------------------------------------------------


def test_identical_labels_agree(task, make_label):
    label = make_label()
    result = task.check_consensus([label, copy.deepcopy(label)])
    assert isinstance(result, ConsensusResult)
    assert result.agreed
    assert result.agreed_label == label
    assert result.rule_version == "ontology-consensus@1"
    assert result.rule_version == task.consensus_rule_version
    assert result.detail["classifications"]["view"] == {"agreed": True, "values": ["PA", "PA"]}
    assert result.detail["masks"]["lung"] == {
        "agreed": True,
        "threshold": 0.8,
        "min_iou": 1.0,
        "pairwise_iou": [[0, 1, 1.0]],
        "sizes": [[10, 10], [10, 10]],
    }


def test_a_classification_mismatch_means_no_agreement(task, make_label):
    first, second = make_label(view="PA"), make_label(view="AP")
    result = task.check_consensus([first, second])
    assert not result.agreed
    assert result.agreed_label is None
    assert result.detail["classifications"]["view"] == {"agreed": False, "values": ["PA", "AP"]}
    # everything else agrees, and the detail still says so
    assert result.detail["classifications"]["quality"]["agreed"]
    assert result.detail["masks"]["lung"]["agreed"]
    assert result.detail["masks"]["effusion"]["agreed"]


def test_classifications_must_match_exactly(task, make_label):
    result = task.check_consensus([make_label(quality="good"), make_label(quality="poor")])
    assert not result.agreed
    assert result.detail["classifications"]["quality"]["values"] == ["good", "poor"]


def test_absent_and_null_optional_classifications_are_the_same(task, make_label):
    absent = make_label(quality=None)
    null = make_label()
    null["classifications"]["quality"] = None
    result = task.check_consensus([absent, null])
    assert result.agreed
    assert result.detail["classifications"]["quality"] == {"agreed": True, "values": [None, None]}


def test_an_optional_classification_given_by_only_one_labeler_disagrees(task, make_label):
    result = task.check_consensus([make_label(quality="good"), make_label(quality=None)])
    assert not result.agreed
    assert result.detail["classifications"]["quality"] == {
        "agreed": False,
        "values": ["good", None],
    }


# --- IoU against the threshold ------------------------------------------------------------
#
# block(n) is a 10 x 10 mask with the first n pixels set, so block(n) against block(100) has
# an IoU of exactly n / 100.


@pytest.mark.parametrize(
    ("pixels", "iou", "agreed"),
    [
        (81, 0.81, True),  # just above the lung threshold of 0.8
        (80, 0.8, True),  # exactly at the threshold: agrees
        (79, 0.79, False),  # just below
    ],
)
def test_lung_iou_against_its_threshold(task, make_label, block, pixels, iou, agreed):
    first, second = make_label(lung=block(100)), make_label(lung=block(pixels))
    result = task.check_consensus([first, second])
    lung = result.detail["masks"]["lung"]
    assert lung["pairwise_iou"] == [[0, 1, iou]]
    assert lung["min_iou"] == iou
    assert lung["threshold"] == LUNG_THRESHOLD
    assert lung["agreed"] is agreed
    assert result.agreed is agreed


@pytest.mark.parametrize(
    ("pixels", "iou", "agreed"), [(51, 0.51, True), (50, 0.5, True), (49, 0.49, False)]
)
def test_each_mask_class_has_its_own_threshold(task, make_label, block, pixels, iou, agreed):
    first, second = make_label(effusion=block(100)), make_label(effusion=block(pixels))
    result = task.check_consensus([first, second])
    effusion = result.detail["masks"]["effusion"]
    assert effusion["threshold"] == EFFUSION_THRESHOLD
    assert effusion["pairwise_iou"] == [[0, 1, iou]]
    assert effusion["agreed"] is agreed
    assert result.detail["masks"]["lung"]["agreed"]  # the lung masks are identical
    assert result.agreed is agreed


def test_the_same_iou_can_agree_for_one_class_and_not_another(task, make_label, block):
    # 60 / 100 = 0.6: enough for effusion (0.5), not enough for lung (0.8)
    result = task.check_consensus(
        [
            make_label(lung=block(100), effusion=block(100)),
            make_label(lung=block(60), effusion=block(60)),
        ]
    )
    assert not result.detail["masks"]["lung"]["agreed"]
    assert result.detail["masks"]["effusion"]["agreed"]
    assert not result.agreed


def test_iou_uses_overlap_not_just_pixel_counts(task, make_label, block):
    # Two masks of 50 pixels each that do not overlap at all have an IoU of 0.
    first = block(50)
    second = np.flipud(block(50))
    assert not (first & second).any()
    result = task.check_consensus([make_label(lung=first), make_label(lung=second)])
    assert result.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 0.0]]
    assert not result.agreed


def test_reported_ious_are_rounded_to_six_places(task, make_label, block):
    result = task.check_consensus([make_label(lung=block(9)), make_label(lung=block(3))])
    assert result.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 0.333333]]
    assert result.detail["masks"]["lung"]["min_iou"] == 0.333333


def test_agreement_uses_the_exact_iou_not_the_rounded_one(task, make_label):
    # 1,999,999 of 2,500,000 pixels overlap: an IoU of 0.7999996, which is below the threshold
    # of 0.8 even though it is reported as 0.8 when rounded to six places.
    full = np.ones((1250, 2000), dtype=bool)
    almost = full.copy()
    almost.ravel()[1_999_999:] = False
    empty = np.zeros_like(full)
    result = task.check_consensus(
        [make_label(lung=full, effusion=empty), make_label(lung=almost, effusion=empty)]
    )
    lung = result.detail["masks"]["lung"]
    assert lung["pairwise_iou"] == [[0, 1, 0.8]]
    assert lung["agreed"] is False
    assert not result.agreed


# --- Empty masks and size mismatches ------------------------------------------------------


def test_two_empty_masks_agree(task, make_label, block):
    first, second = make_label(lung=block(0)), make_label(lung=block(0))
    result = task.check_consensus([first, second])
    assert result.agreed
    assert result.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 1.0]]
    assert result.detail["masks"]["lung"]["min_iou"] == 1.0


def test_an_empty_mask_against_a_non_empty_one_disagrees(task, make_label, block):
    first, second = make_label(lung=block(0)), make_label(lung=block(1))
    result = task.check_consensus([first, second])
    assert not result.agreed
    lung = result.detail["masks"]["lung"]
    assert lung["pairwise_iou"] == [[0, 1, 0.0]]
    assert lung["min_iou"] == 0.0
    assert not lung["agreed"]
    # the same the other way round
    assert not task.check_consensus([second, first]).agreed


def test_masks_of_different_sizes_disagree(task, make_label, block):
    first = make_label(lung=block(100), effusion=block(0))
    second = make_label(lung=block(25, 5, 5), effusion=block(0, 5, 5))
    result = task.check_consensus([first, second])
    assert not result.agreed
    lung = result.detail["masks"]["lung"]
    assert lung["agreed"] is False
    assert lung["min_iou"] is None
    assert lung["pairwise_iou"] == [[0, 1, None]]
    assert lung["sizes"] == [[10, 10], [5, 5]]
    # even two empty masks of different sizes disagree
    assert result.detail["masks"]["effusion"]["agreed"] is False


def test_the_same_pixels_in_a_different_shape_disagree(task, make_label, block):
    first = make_label(lung=block(100, 10, 10), effusion=block(0, 10, 10))
    second = make_label(lung=block(100, 5, 20), effusion=block(0, 5, 20))
    assert not task.check_consensus([first, second]).agreed


# --- Three or more labels -----------------------------------------------------------------


def test_three_labels_where_one_pair_fails(task, make_label, block):
    # Labels 0 and 1 agree (80 / 100), labels 0 and 2 agree (64 / 80), but 1 and 2 do not
    # (64 / 100), so there is no consensus: every pair must clear the threshold.
    labels = [
        make_label(lung=block(80)),
        make_label(lung=block(100)),
        make_label(lung=block(64)),
    ]
    result = task.check_consensus(labels)
    assert not result.agreed
    assert result.agreed_label is None
    lung = result.detail["masks"]["lung"]
    assert lung["pairwise_iou"] == [[0, 1, 0.8], [0, 2, 0.8], [1, 2, 0.64]]
    assert lung["min_iou"] == 0.64
    assert lung["agreed"] is False
    assert result.detail["masks"]["effusion"]["agreed"] is True


def test_three_labels_that_all_agree(task, make_label, block):
    labels = [make_label(lung=block(100)), make_label(lung=block(90)), make_label(lung=block(85))]
    result = task.check_consensus(labels)
    assert result.agreed
    lung = result.detail["masks"]["lung"]
    assert [pair[:2] for pair in lung["pairwise_iou"]] == [[0, 1], [0, 2], [1, 2]]
    assert lung["min_iou"] == 0.85
    assert lung["sizes"] == [[10, 10]] * 3
    assert result.detail["classifications"]["view"]["values"] == ["PA", "PA", "PA"]


def test_three_labels_where_one_classification_differs(task, make_label):
    labels = [make_label(view="PA"), make_label(view="PA"), make_label(view="lateral")]
    result = task.check_consensus(labels)
    assert not result.agreed
    assert result.detail["classifications"]["view"] == {
        "agreed": False,
        "values": ["PA", "PA", "lateral"],
    }


def test_three_labels_one_of_a_different_size(task, make_label, block):
    labels = [
        make_label(),
        make_label(),
        make_label(lung=block(100, 5, 20), effusion=block(0, 5, 20)),
    ]
    result = task.check_consensus(labels)
    assert not result.agreed
    lung = result.detail["masks"]["lung"]
    assert lung["min_iou"] is None
    assert lung["pairwise_iou"] == [[0, 1, 1.0], [0, 2, None], [1, 2, None]]


# --- The agreed label ---------------------------------------------------------------------


def test_the_agreed_label_is_a_copy_of_the_first_not_a_merge(task, make_label, block):
    first = make_label(lung=block(100), quality="good")
    second = make_label(lung=block(90), quality="good")
    third = make_label(lung=block(85), quality="good")
    result = task.check_consensus([first, second, third])
    assert result.agreed
    assert result.agreed_label == first
    assert result.agreed_label != second
    assert result.agreed_label["masks"]["lung"] == rle.encode(block(100))


def test_the_agreed_label_is_the_earliest_submitted_one_in_the_order_given(task, make_label, block):
    big, small = make_label(lung=block(100)), make_label(lung=block(90))
    assert task.check_consensus([big, small]).agreed_label == big
    assert task.check_consensus([small, big]).agreed_label == small


def test_changing_the_agreed_label_does_not_change_the_input(task, make_label):
    first, second = make_label(), make_label()
    snapshot = copy.deepcopy(first)
    result = task.check_consensus([first, second])
    assert result.agreed_label is not first
    result.agreed_label["classifications"]["view"] = "lateral"
    result.agreed_label["masks"]["lung"]["counts"] = "changed"
    del result.agreed_label["masks"]["effusion"]
    assert first == snapshot
    assert second == snapshot


def test_changing_the_input_afterwards_does_not_change_the_agreed_label(task, make_label):
    first, second = make_label(), make_label()
    result = task.check_consensus([first, second])
    first["classifications"]["view"] = "lateral"
    first["masks"]["lung"]["size"][0] = 99
    assert result.agreed_label["classifications"]["view"] == "PA"
    assert result.agreed_label["masks"]["lung"]["size"] == [10, 10]


def test_consensus_does_not_change_the_labels_it_is_given(task, make_label, block):
    labels = [make_label(lung=block(100)), make_label(lung=block(10))]
    snapshot = copy.deepcopy(labels)
    task.check_consensus(labels)
    assert labels == snapshot


# --- The detail is plain JSON -------------------------------------------------------------


def test_the_detail_is_json_compatible(task, make_label, block):
    labels = [make_label(lung=block(9)), make_label(lung=block(3)), make_label(view="AP")]
    detail = task.check_consensus(labels).detail
    assert json.loads(json.dumps(detail)) == detail  # nothing is lost, and no tuples
    assert_plain_json(detail)


def test_the_detail_is_json_compatible_when_sizes_differ(task, make_label, block):
    labels = [make_label(), make_label(lung=block(100, 5, 20), effusion=block(0, 5, 20))]
    detail = task.check_consensus(labels).detail
    assert json.loads(json.dumps(detail)) == detail
    assert_plain_json(detail)


def test_the_detail_lists_every_field(task, make_label):
    detail = task.check_consensus([make_label(), make_label()]).detail
    assert list(detail) == ["classifications", "masks"]
    assert list(detail["classifications"]) == ["view", "quality"]
    assert list(detail["masks"]) == ["lung", "effusion"]


# --- Fewer than two labels ----------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1])
def test_consensus_needs_at_least_two_labels(task, make_label, count):
    with pytest.raises(ValueError, match=f"at least two labels, but got {count}"):
        task.check_consensus([make_label() for _ in range(count)])


# --- Scoring against a key ----------------------------------------------------------------


def test_a_label_that_matches_the_key_scores_as_agreeing(task, make_label):
    result = task.score(make_label(), make_label())
    assert result.agreed
    assert result.rule_version == task.consensus_rule_version


def test_scoring_is_consensus_on_the_label_and_the_key(task, make_label, block):
    label, key = make_label(lung=block(90)), make_label(lung=block(100))
    scored = task.score(label, key)
    assert scored == task.check_consensus([label, key])
    assert scored.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 0.9]]
    assert scored.agreed_label == label  # the label is first, the key second


def test_a_label_that_misses_the_key_does_not_score_as_agreeing(task, make_label, block):
    key = make_label(view="PA", lung=block(100))
    wrong_view = task.score(make_label(view="AP"), key)
    assert not wrong_view.agreed
    assert wrong_view.detail["classifications"]["view"]["values"] == ["AP", "PA"]
    poor_mask = task.score(make_label(lung=block(50)), key)
    assert not poor_mask.agreed
    assert poor_mask.detail["masks"]["lung"]["pairwise_iou"] == [[0, 1, 0.5]]


def test_scoring_uses_the_same_thresholds(task, make_label, block):
    key = make_label(lung=block(100), effusion=block(100))
    assert task.score(make_label(lung=block(80), effusion=block(50)), key).agreed
    assert not task.score(make_label(lung=block(79), effusion=block(50)), key).agreed
    assert not task.score(make_label(lung=block(80), effusion=block(49)), key).agreed


def test_an_invalid_key_is_reported_as_the_key_not_the_label(task, make_label):
    bad_key = make_label(view="sideways")
    with pytest.raises(LabelValidationError) as info:
        task.score(make_label(), bad_key)
    assert info.value.problems == [
        "The key is invalid. Classification 'view' has the value 'sideways', which is not "
        "allowed. Allowed values: 'PA', 'AP', 'lateral'."
    ]
    assert str(info.value).startswith("The key is invalid.")


def test_every_problem_with_the_key_is_listed(task, make_label):
    bad_key = {"classifications": {"veiw": "PA"}, "masks": {}}
    with pytest.raises(LabelValidationError) as info:
        task.score(make_label(), bad_key)
    assert len(info.value.problems) == 4  # unknown name, missing view, two missing masks
    assert all(problem.startswith("The key is invalid. ") for problem in info.value.problems)


def test_a_valid_key_is_not_changed_by_scoring(task, make_label):
    key = make_label()
    snapshot = copy.deepcopy(key)
    task.score(make_label(), key)
    assert key == snapshot


# --- Properties with random masks ---------------------------------------------------------


@st.composite
def label_pairs(draw):
    """Two valid labels for the example ontology, with random masks of one shared shape."""
    shape = draw(st.tuples(st.integers(1, 8), st.integers(1, 8)))
    masks = arrays(dtype=bool, shape=shape)
    views = st.sampled_from(["PA", "AP", "lateral"])
    qualities = st.sampled_from(["good", "poor", None])
    labels = []
    for _ in range(2):
        classifications = {"view": draw(views)}
        if (quality := draw(qualities)) is not None:
            classifications["quality"] = quality
        labels.append(
            {
                "classifications": classifications,
                "masks": {
                    "lung": rle.encode(draw(masks), compressed=draw(st.booleans())),
                    "effusion": rle.encode(draw(masks), compressed=draw(st.booleans())),
                },
            }
        )
    return labels


def iou(first: np.ndarray, second: np.ndarray) -> float:
    union = (first | second).sum()
    return 1.0 if union == 0 else (first & second).sum() / union


@given(label_pairs())
def test_valid_random_labels_validate_to_equal_copies(labels):
    for label in labels:
        assert TASK.validate(label) == label


@given(label_pairs())
def test_consensus_matches_a_plain_numpy_calculation(labels):
    first, second = labels
    result = TASK.check_consensus(labels)

    expected = True
    for name in ("view", "quality"):
        expected &= first["classifications"].get(name) == second["classifications"].get(name)
    for name, threshold in (("lung", LUNG_THRESHOLD), ("effusion", EFFUSION_THRESHOLD)):
        value = iou(rle.decode(first["masks"][name]), rle.decode(second["masks"][name]))
        expected &= value >= threshold
        assert result.detail["masks"][name]["pairwise_iou"] == [[0, 1, round(float(value), 6)]]
    assert result.agreed == expected
    assert (result.agreed_label == first) if expected else (result.agreed_label is None)


@given(label_pairs())
def test_agreement_does_not_depend_on_the_order_of_the_labels(labels):
    forward = TASK.check_consensus(labels)
    backward = TASK.check_consensus(labels[::-1])
    assert forward.agreed == backward.agreed
    for name in ("lung", "effusion"):
        assert forward.detail["masks"][name]["min_iou"] == backward.detail["masks"][name]["min_iou"]


@given(label_pairs())
def test_a_label_always_agrees_with_itself(labels):
    assert TASK.check_consensus([labels[0], labels[0]]).agreed
    assert TASK.score(labels[0], labels[0]).agreed
