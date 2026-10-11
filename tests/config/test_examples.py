"""The example workflows in examples/workflows must stay valid."""

from pathlib import Path

import pytest

from labelflow.engine.config import ValidationContext, validate_config

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "workflows"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.yaml"))


def load(name):
    return validate_config((EXAMPLES_DIR / f"{name}.yaml").read_text()).config


def test_the_three_examples_from_the_design_note_exist():
    assert {path.stem for path in EXAMPLE_FILES} >= {
        "review_sample",
        "adjudicate",
        "training_round",
    }


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_example_has_no_errors_and_no_warnings(path):
    report = validate_config(path.read_text())
    assert report.errors == [], report.render()
    assert report.warnings == [], report.render()
    assert report.config.label_task == "image_ontology@1"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_example_has_no_warnings_when_the_roles_have_enough_people(path):
    config = validate_config(path.read_text()).config
    roles = {role: ["ana", "bo", "cy"] for stage in config.stages for role in stage.roles}
    report = validate_config(path.read_text(), ValidationContext(roles=roles))
    assert report.ok and report.warnings == []


def test_review_sample_means_what_the_design_note_says():
    config = load("review_sample")
    review = config.stage("expert_review")
    assert review.sample == 0.2
    assert review.roles == ("expert",)
    assert config.resolved_final() == ("expert_review", "annotate")


def test_adjudicate_takes_only_the_disagreements():
    config = load("adjudicate")
    assert config.stage("annotate").labels_per_item == 2
    assert config.stage("adjudicate").input == "annotate.disagreed"
    assert config.resolved_final() == ("adjudicate", "annotate")


def test_training_round_has_no_final_labels_and_scores_against_the_key():
    config = load("training_round")
    stage = config.stage("gold_read")
    assert stage.labels_per_item == "all"
    assert stage.score_against == "gold_key"
    assert config.resolved_final() is None
