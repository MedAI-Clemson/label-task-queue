"""The pilot's ontology and workflow files, checked with labelflow's own validators, and the
deterministic split of the images into the gold set and the main set."""

from __future__ import annotations

from pathlib import Path

import pytest

from labelflow.engine.config import ValidationContext, validate_config
from labelflow.labeltask import registry_from_directory

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "pilot" / "jsrt"
TRAINING = (PILOT / "workflows" / "training.yaml").read_text()
MAIN = (PILOT / "workflows" / "main.yaml").read_text()


def context(experts: list[str]) -> ValidationContext:
    return ValidationContext(
        roles={"trainee": ["tara", "tom"], "annotator": ["ana", "ben"], "expert": experts},
        datasets=["jsrt_gold", "jsrt_main"],
        label_tasks=registry_from_directory(PILOT / "ontology").refs(),
    )


def test_the_ontology_folder_loads_as_the_server_loads_it():
    registry = registry_from_directory(PILOT / "ontology")
    assert registry.refs() == ["jsrt_scr@1"]
    ontology = registry.get("jsrt_scr@1").ontology
    assert [m.name for m in ontology.masks] == [
        "right_lung",
        "left_lung",
        "heart",
        "right_clavicle",
        "left_clavicle",
    ]
    # thresholds a little below the SCR paper's human-observer overlap (0.946, 0.878, 0.896)
    thresholds = {m.name: m.threshold for m in ontology.masks}
    assert thresholds["right_lung"] < 0.946 and thresholds["heart"] < 0.878
    assert thresholds["left_clavicle"] < 0.896
    assert ontology.max_mask_pixels == 1024 * 1024
    assert [(c.name, c.values, c.required) for c in ontology.classifications] == [
        ("nodule", ("present", "absent"), True)
    ]


@pytest.mark.parametrize("text", [TRAINING, MAIN], ids=["training", "main"])
def test_the_workflows_are_valid_without_warnings_when_staffed(text):
    assert validate_config(text).errors == []
    report = validate_config(text, context(["dr_eve", "dr_fay"]))
    assert report.ok, report.render()
    assert report.warnings == [], report.render()


def test_the_main_workflow_warns_with_a_single_expert():
    report = validate_config(MAIN, context(["dr_eve"]))
    assert report.ok
    [warning] = report.warnings
    assert "'adjudicate' and 'expert_review' can both hold the same item" in warning.message


def test_the_training_round_means_what_its_comments_say():
    config = validate_config(TRAINING).config
    [stage] = config.stages
    assert (stage.labels_per_item, stage.score_against, stage.show_prior) == (
        "all",
        "gold_key",
        False,
    )
    assert config.resolved_final() is None
    assert (config.dataset, config.label_task) == ("jsrt_gold", "jsrt_scr@1")


def test_the_main_workflow_means_what_its_comments_say():
    config = validate_config(MAIN).config
    annotate, adjudicate, review = config.stages
    assert annotate.labels_per_item.count == 2 and annotate.labels_per_item.fraction == 0.2
    assert adjudicate.input == "annotate.disagreed" and adjudicate.show_prior
    assert (review.input, review.sample, review.show_prior) == ("annotate", 0.1, False)
    assert {s.score_against for s in config.stages} == {"gold_key"}
    assert config.resolved_final() == ("adjudicate", "expert_review", "annotate")
    assert (config.dataset, config.label_task) == ("jsrt_main", "jsrt_scr@1")


def items(present: int, absent: int) -> list[dict]:
    made = [{"item": f"JPCLN{i:03d}", "properties": {"nodule": "present"}} for i in range(present)]
    made += [{"item": f"JPCNN{i:03d}", "properties": {"nodule": "absent"}} for i in range(absent)]
    return made


def test_the_gold_split_is_stratified_and_deterministic(setup_module):
    everything = items(154, 93)  # the JSRT's counts
    gold, main = setup_module.split_items(everything, 20, seed=20261011)
    assert len(gold) == 20 and len(main) == 227
    assert sum(i["properties"]["nodule"] == "present" for i in gold) == 12  # 20 x 154/247
    assert {i["item"] for i in gold}.isdisjoint(i["item"] for i in main)
    assert setup_module.split_items(everything, 20, seed=20261011) == (gold, main)
    other, _ = setup_module.split_items(everything, 20, seed=1)
    assert other != gold
