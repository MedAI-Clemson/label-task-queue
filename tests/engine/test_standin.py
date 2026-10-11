"""The stand-in label task used by the engine tests and simulations."""

import pytest

from labelflow.labeltask import LabelTaskRegistry, LabelValidationError
from labelflow.labeltask.standin import StandInLabelTask


def test_validate():
    task = StandInLabelTask()
    assert task.validate({"value": "cat"}) == {"value": "cat"}
    with pytest.raises(LabelValidationError) as caught:
        task.validate({"value": 3, "extra": 1})
    assert caught.value.problems == [
        "Unknown field 'extra'.",
        "The field 'value' must be a non-empty string.",
    ]
    with pytest.raises(LabelValidationError, match="object"):
        task.validate("cat")


def test_consensus_means_equal_values_and_copies_the_first_label():
    task = StandInLabelTask()
    first = {"value": "cat"}
    agreed = task.check_consensus([first, {"value": "cat"}, {"value": "cat"}])
    assert agreed.agreed and agreed.agreed_label == first and agreed.agreed_label is not first
    assert agreed.rule_version == task.consensus_rule_version == "equal-values@1"
    disagreed = task.check_consensus([first, {"value": "dog"}])
    assert not disagreed.agreed and disagreed.agreed_label is None
    assert disagreed.detail == {"values": ["cat", "dog"]}
    with pytest.raises(ValueError, match="two or more"):
        task.check_consensus([first])


def test_scoring_uses_the_same_check():
    task = StandInLabelTask()
    assert task.score({"value": "cat"}, {"value": "cat"}).agreed
    assert not task.score({"value": "cat"}, {"value": "dog"}).agreed


def test_name_and_version_are_configurable():
    task = StandInLabelTask(name="other", version=3, rule="strict@2")
    assert task.ref == "other@3"
    assert task.ontology_version == "other-ontology@3"
    assert task.check_consensus([{"value": "a"}, {"value": "a"}]).rule_version == "strict@2"
    assert "other@3" in LabelTaskRegistry([task, StandInLabelTask()])
