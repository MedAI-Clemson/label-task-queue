"""The Montgomery kit's ontology, workflow files and example accounts, checked with
labelflow's own validators, and the split into the gold set and the main set."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from labelflow.engine.config import ValidationContext, validate_config
from labelflow.labeltask import registry_from_directory

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "pilot" / "montgomery"
TRAINING = (KIT / "workflows" / "training.yaml").read_text()
MAIN = (KIT / "workflows" / "main.yaml").read_text()


def context(experts: list[str]) -> ValidationContext:
    return ValidationContext(
        roles={
            "trainee": ["trainee_one", "trainee_two", "trainee_three"],
            "annotator": ["annotator_one", "annotator_two", "annotator_three"],
            "expert": experts,
        },
        datasets=["montgomery_gold", "montgomery_main"],
        label_tasks=registry_from_directory(KIT / "ontology").refs(),
    )


def test_the_ontology_folder_loads_as_the_server_loads_it():
    registry = registry_from_directory(KIT / "ontology")
    assert registry.refs() == ["montgomery_tb@1"]
    ontology = registry.get("montgomery_tb@1").ontology
    assert [(m.name, m.threshold) for m in ontology.masks] == [
        ("right_lung", 0.90),
        ("left_lung", 0.90),
    ]
    # a little below the SCR paper's human-observer lung overlap (0.946 +- 0.018)
    assert all(0.946 - 3 * 0.018 < m.threshold < 0.946 for m in ontology.masks)
    assert ontology.max_mask_pixels == 1024 * 841  # one display-size image
    assert [(c.name, c.values, c.required) for c in ontology.classifications] == [
        ("tb_findings", ("present", "absent"), True)
    ]


@pytest.mark.parametrize("text", [TRAINING, MAIN], ids=["training", "main"])
def test_the_workflows_are_valid_without_warnings_when_staffed(text):
    assert validate_config(text).errors == []
    report = validate_config(text, context(["expert_one", "expert_two"]))
    assert report.ok, report.render()
    assert report.warnings == [], report.render()


def test_the_main_workflow_warns_with_a_single_expert():
    report = validate_config(MAIN, context(["expert_one"]))
    assert report.ok
    [warning] = report.warnings
    assert "'adjudicate' and 'expert_review' can both hold the same item" in warning.message


def test_the_workflows_mean_what_their_comments_say():
    training = validate_config(TRAINING).config
    [stage] = training.stages
    assert (stage.labels_per_item, stage.score_against, stage.show_prior) == (
        "all",
        "gold_key",
        False,
    )
    assert training.resolved_final() is None
    assert (training.dataset, training.label_task) == ("montgomery_gold", "montgomery_tb@1")

    main = validate_config(MAIN).config
    annotate, adjudicate, review = main.stages
    assert annotate.labels_per_item.count == 2 and annotate.labels_per_item.fraction == 0.2
    assert adjudicate.input == "annotate.disagreed" and adjudicate.show_prior
    assert (review.input, review.sample, review.show_prior) == ("annotate", 0.1, False)
    assert {s.score_against for s in main.stages} == {"gold_key"}
    assert main.resolved_final() == ("adjudicate", "expert_review", "annotate")
    assert (main.dataset, main.label_task) == ("montgomery_main", "montgomery_tb@1")


def test_the_gold_split_is_stratified_and_deterministic(mc_setup):
    answers = ["present"] * 58 + ["absent"] * 80  # the dataset's counts
    items = [
        {"item": f"mc_{i:010x}", "properties": {"tb_findings": a}} for i, a in enumerate(answers)
    ]
    gold, main = mc_setup.split_items(items, mc_setup.KIT.gold, seed=20261011)
    assert len(gold) == 12 and len(main) == 126
    assert sum(i["properties"]["tb_findings"] == "present" for i in gold) == 5  # 12 x 58/138
    assert {i["item"] for i in gold}.isdisjoint(i["item"] for i in main)
    assert mc_setup.split_items(items, 12, seed=20261011) == (gold, main)


def test_the_example_accounts(mc_setup):
    path = KIT / "accounts.example.csv"
    people = mc_setup.read_accounts(path)
    roles = sorted(r for p in people for r in p["roles"])
    assert roles == ["annotator"] * 3 + ["expert"] * 2 + ["trainee"] * 3
    assert [p["username"] for p in people if p["is_admin"]] == ["pilot_admin"]
    # no passwords in the repository: every one is generated at setup
    with path.open(newline="") as handle:
        assert "password" not in next(csv.reader(handle))
    assert all(p["password"] == "" for p in people)


def test_the_split_keeps_each_patients_images_on_one_side(mc_setup):
    # 58 TB and 80 normal images, as in the dataset; some TB patients have 2 or 3 images,
    # linked as prepare.py links them (one way only, and through chains)
    answers = ["present"] * 58 + ["absent"] * 80
    items = [
        {"item": f"mc_{i:010x}", "properties": {"tb_findings": a, "same_patient_as": []}}
        for i, a in enumerate(answers)
    ]
    patients = [[0, 1], [2, 3, 4], [5, 6], [7, 8, 9], [10, 11]]
    for _first, *others in patients:
        for other in others:  # each later image names the one before it
            items[other]["properties"]["same_patient_as"] = [items[other - 1]["item"]]
    assert {len(u) for u in mc_setup.kit_setup.patient_units(items, "same_patient_as")} == {
        1,
        2,
        3,
    }
    for seed in range(40):
        gold, main = mc_setup.split_items(items, 12, seed=seed)
        assert len(gold) == 12 and len(main) == 126
        assert sum(i["properties"]["tb_findings"] == "present" for i in gold) == 5
        in_gold = {i["item"] for i in gold}
        for patient in patients:
            sides = {items[n]["item"] in in_gold for n in patient}
            assert len(sides) == 1, (seed, patient)
        assert mc_setup.split_items(items, 12, seed=seed) == (gold, main)
    # the seeds draw patients with several images into the gold set too, whole
    drawn = set()
    for seed in range(40):
        in_gold = {i["item"] for i in mc_setup.split_items(items, 12, seed=seed)[0]}
        drawn |= {tuple(p) for p in patients if items[p[0]]["item"] in in_gold}
    assert drawn


def test_a_split_with_no_links_is_the_plain_stratified_draw(mc_setup):
    items = [
        {"item": f"mc_{i:010x}", "properties": {"tb_findings": "present" if i % 3 else "absent"}}
        for i in range(30)
    ]
    linked = mc_setup.split_items(items, 9, seed=5)
    plain = mc_setup.kit_setup.split_items(items, 9, 5, "tb_findings")
    assert linked == plain
