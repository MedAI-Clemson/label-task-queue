"""Workflows: check a config, start it, look at it, preview and make edits."""

from __future__ import annotations

import pytest
import yaml

from labelflow.engine import draw

REVIEW = yaml.safe_load(
    """
workflow: xray_main
dataset: batch1
label_task: standin@1
stages:
  - name: annotate
    assign_to: annotator
  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2
final: [expert_review, annotate]
"""
)


def edited(**changes):
    """The review config with the review stage's settings changed."""
    config = yaml.safe_load(yaml.safe_dump(REVIEW))
    config["stages"][1].update(changes)
    return config


# --- validate ----------------------------------------------------------------------------


def test_a_valid_yaml_config_is_checked(admin, project, expect):
    text = yaml.safe_dump(REVIEW, sort_keys=False)
    result = expect(admin.post(f"{project}/workflows/validate", {"config": text}))
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["diagram"].startswith("flowchart TD")
    assert "sample: 0.2" in result["resolved_yaml"]
    assert "labels_per_item" in result["resolved_yaml"]  # defaults are written out
    assert result["report"].startswith("The config is valid.")


def test_a_json_config_gives_the_same_answer_as_yaml(admin, project, expect):
    from_yaml = expect(
        admin.post(f"{project}/workflows/validate", {"config": yaml.safe_dump(REVIEW)})
    )
    from_json = expect(admin.post(f"{project}/workflows/validate", {"config": REVIEW}))
    assert from_yaml == from_json
    assert from_json["ok"] is True


def test_validation_warns_about_roles_that_are_too_small(admin, project, expect):
    config = edited(labels_per_item=2)
    result = expect(admin.post(f"{project}/workflows/validate", {"config": config}))
    assert result["ok"] is True  # a warning does not make a config invalid
    (warning,) = result["warnings"]
    assert warning["where"] == "stage 'expert_review'"
    assert "needs 2 different people" in warning["message"]
    assert "only 1 person holds the role 'expert'" in warning["message"]


def test_validation_uses_the_roles_datasets_and_label_tasks_of_the_project(admin, project, expect):
    config = edited(assign_to="chief")
    config["dataset"] = "batch9"
    config["label_task"] = "image_ontology@1"
    result = expect(admin.post(f"{project}/workflows/validate", {"config": config}))
    assert result["ok"] is False
    assert result["resolved_yaml"] is None
    assert result["diagram"] is None
    assert len(result["errors"]) == 3
    messages = " ".join(e["message"] for e in result["errors"])
    assert "The dataset 'batch9' does not exist in this project." in messages
    assert "The label task 'image_ontology@1' does not exist in this project." in messages
    assert "The role 'chief' does not exist in this project." in messages
    assert result["report"].startswith("The config has 3 errors.")


def test_a_config_with_broken_yaml_is_reported_not_raised(admin, project, expect):
    result = expect(admin.post(f"{project}/workflows/validate", {"config": "workflow: [oops"}))
    assert result["ok"] is False
    assert result["errors"][0]["message"].startswith("The file is not valid YAML.")


def test_a_config_must_be_text_or_an_object(admin, project):
    response = admin.post(f"{project}/workflows/validate", {"config": 42})
    assert response.status_code == 422
    assert admin.post(f"{project}/workflows/validate", {}).status_code == 422


# --- start -------------------------------------------------------------------------------


def test_starting_a_workflow(admin, project, expect):
    result = expect(admin.post(f"{project}/workflows", {"config": REVIEW, "seed": 7}), 201)
    assert result == {"workflow": "xray_main", "seed": 7, "config_version": 1, "warnings": []}
    detail = expect(admin.get(f"{project}/workflows/xray_main"))
    assert detail["seed"] == 7
    assert detail["progress"]["items"]["in_progress"] == 20


def test_a_workflow_started_from_yaml_text(admin, project, expect):
    text = yaml.safe_dump(REVIEW, sort_keys=False)
    expect(admin.post(f"{project}/workflows", {"config": text, "seed": 1}), 201)


def test_the_seed_is_chosen_at_random_when_not_given(admin, project, expect):
    seeds = set()
    for name in ("one", "two", "three"):
        config = REVIEW | {"workflow": name}
        result = expect(admin.post(f"{project}/workflows", {"config": config}), 201)
        assert 0 <= result["seed"] < 2**63
        assert expect(admin.get(f"{project}/workflows/{name}"))["seed"] == result["seed"]
        seeds.add(result["seed"])
    assert len(seeds) == 3


def test_the_seed_fixes_the_draws(admin, ana, project, label_with, expect):
    expect(admin.post(f"{project}/workflows", {"config": REVIEW, "seed": 7}), 201)
    for _ in range(5):
        label_with(ana, f"{project}/workflows/xray_main")
    admitted = expect(admin.get(f"{project}/events?type=admission_decided&limit=500"))["events"]
    reviews = [e for e in admitted if e["stage"] == "expert_review"]
    assert len(reviews) == 5
    for event in reviews:
        assert event["draw"] == draw(7, "expert_review", event["item"])
        assert event["threshold"] == 0.2


@pytest.mark.parametrize("seed", [-1, 2**63, "seven", 1.5])
def test_a_bad_seed_is_refused(admin, project, seed):
    response = admin.post(f"{project}/workflows", {"config": REVIEW, "seed": seed})
    assert response.status_code == 422


def test_an_invalid_config_is_refused_with_every_problem(admin, project):
    config = edited(assign_to="chief")
    config["dataset"] = "batch9"
    config["label_task"] = "other@1"
    response = admin.post(f"{project}/workflows", {"config": config})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"].startswith("The config has 3 errors. 1. ")
    assert len(body["problems"]) == 3
    assert all(body["detail"].count(p) == 1 for p in body["problems"])
    assert admin.get(f"{project}/workflows").json() == []  # nothing was started


def test_a_config_with_a_mistake_in_a_key_is_refused(admin, project):
    response = admin.post(f"{project}/workflows", {"config": edited(sample=3)})
    assert response.status_code == 422
    assert "sample is a fraction, not a percentage" in response.json()["detail"]


def test_starting_a_workflow_twice_is_a_conflict(admin, workflow, project):
    response = admin.post(f"{project}/workflows", {"config": REVIEW})
    assert response.status_code == 409
    assert response.json()["detail"] == "A workflow named 'xray_main' already exists."


def test_starting_warns_about_roles_that_are_too_small(admin, project, expect):
    config = REVIEW | {"workflow": "pairs"}
    config["stages"] = [{"name": "annotate", "assign_to": "expert", "labels_per_item": 2}]
    config["final"] = ["annotate"]
    result = expect(admin.post(f"{project}/workflows", {"config": config}), 201)
    (warning,) = result["warnings"]
    assert warning.startswith("workflow pairs, stage 'annotate'")


# --- list and show -----------------------------------------------------------------------


def test_an_admin_lists_every_workflow(admin, workflow, project, expect):
    expect(admin.post(f"{project}/workflows", {"config": REVIEW | {"workflow": "second"}}), 201)
    listed = expect(admin.get(f"{project}/workflows"))
    assert [w["workflow"] for w in listed] == ["xray_main", "second"]
    first = listed[0]
    assert first["dataset"] == "batch1"
    assert first["label_task"] == "standin@1"
    assert first["status"] == "running"
    assert first["active_version"] == 1
    assert first["started_by"] == "root"
    assert first["stages"] == [
        {"name": "annotate", "assign_to": ["annotator"]},
        {"name": "expert_review", "assign_to": ["expert"]},
    ]


def test_a_labeler_lists_the_workflows_where_they_hold_a_role(
    admin, ana, dr_smith, project, expect
):
    expect(admin.post(f"{project}/roles", {"role": "trainee"}), 201)
    only_trainees = REVIEW | {"workflow": "training"}
    only_trainees["stages"] = [{"name": "gold", "assign_to": "trainee", "labels_per_item": "all"}]
    only_trainees["final"] = "none"
    expect(admin.post(f"{project}/workflows", {"config": REVIEW}), 201)
    expect(admin.post(f"{project}/workflows", {"config": only_trainees}), 201)
    assert [w["workflow"] for w in expect(ana.get(f"{project}/workflows"))] == ["xray_main"]
    assert [w["workflow"] for w in expect(dr_smith.get(f"{project}/workflows"))] == ["xray_main"]
    grant = {"user": "ana", "reason": "training"}
    expect(admin.post(f"{project}/roles/trainee/grant", grant))
    assert [w["workflow"] for w in expect(ana.get(f"{project}/workflows"))] == [
        "xray_main",
        "training",
    ]


def test_a_stranger_sees_no_workflows(admin, new_person, workflow, project, expect):
    assert expect(new_person("stranger").get(f"{project}/workflows")) == []


def test_showing_a_workflow(admin, workflow, expect):
    detail = expect(admin.get(workflow))
    assert detail["workflow"] == "xray_main"
    assert detail["seed"] == 11
    assert detail["active_version"] == 1
    assert detail["diagram"].startswith("flowchart TD")
    assert "workflow: xray_main" in detail["resolved_yaml"]
    assert detail["progress"] == {
        "items": {
            "entered": 20,
            "in_progress": 20,
            "final": 0,
            "unresolved": 0,
            "none": 0,
            "excluded": 0,
        },
        "stages": [
            {"stage": "annotate", "open": 20, "claimed": 0, "filled": 0, "cancelled": 0},
            {"stage": "expert_review", "open": 0, "claimed": 0, "filled": 0, "cancelled": 0},
        ],
    }


def test_progress_follows_the_work(admin, ana, workflow, label_with, expect):
    work = label_with(ana, workflow)
    claim = expect(ana.post(f"{workflow}/next"))
    stages = {s["stage"]: s for s in expect(admin.get(workflow))["progress"]["stages"]}
    assert stages["annotate"]["filled"] == 1
    assert stages["annotate"]["claimed"] == 1
    assert stages["annotate"]["open"] == 18
    assert work["slot"]["slot"] != claim["slot"]["slot"]


def test_a_labeler_sees_a_workflow_they_hold_a_role_in_but_not_its_seed(
    admin, ana, workflow, expect
):
    seen = expect(ana.get(workflow))
    assert seen["workflow"] == "xray_main"
    assert seen["seed"] is None  # it would tell labelers which of their items get reviewed
    assert expect(admin.get(workflow))["seed"] == 11
    assert {k: v for k, v in seen.items() if k != "seed"} == {
        k: v for k, v in expect(admin.get(workflow)).items() if k != "seed"
    }


def test_a_labeler_without_a_role_in_the_workflow_may_not_see_it(
    admin, new_person, workflow, expect
):
    response = new_person("stranger").get(workflow)
    assert response.status_code == 403
    assert response.json()["detail"] == "You do not hold a role in this workflow."


def test_an_unknown_workflow_is_not_found(admin, ana, project):
    for who in (admin, ana):
        response = who.get(f"{project}/workflows/nope")
        assert response.status_code == 404
        assert response.json()["detail"] == "There is no workflow named 'nope'."


# --- versions, preview and edits ---------------------------------------------------------


def test_a_new_workflow_has_one_version(admin, workflow, expect):
    (version,) = expect(admin.get(f"{workflow}/versions"))
    assert version["version"] == 1
    assert version["active"] is True
    assert version["activated_by"] == "root"
    assert version["reason"] is None
    assert version["diff"] is None
    assert "sample: 0.2" in version["resolved_yaml"]
    assert len(version["config_hash"]) == 64


def test_previewing_an_edit(admin, workflow, expect):
    result = expect(admin.post(f"{workflow}/preview-edit", {"config": edited(sample=0.3)}))
    assert result["can_activate"] is True
    preview = result["preview"]
    assert (preview["base_version"], preview["new_version"]) == (1, 2)
    assert [c["summary"] for c in preview["changes"]] == ["expert_review.sample: 0.2 -> 0.3"]
    assert preview["errors"] == []
    assert preview["items"] == {"entered": 20, "in_progress": 20, "finished": 0, "excluded": 0}
    (impact,) = preview["stages"]
    assert impact["stage"] == "expert_review"
    assert impact["may_arrive"] == 20
    assert impact["admitted_at_old_sample"] <= impact["admitted_at_new_sample"]
    old = sum(draw(11, "expert_review", f"img_{n:03d}") < 0.2 for n in range(1, 21))
    new = sum(draw(11, "expert_review", f"img_{n:03d}") < 0.3 for n in range(1, 21))
    assert (impact["admitted_at_old_sample"], impact["admitted_at_new_sample"]) == (old, new)
    assert result["text"].startswith("Change preview for workflow 'xray_main'")
    assert "This edit can be activated." in result["text"]
    # a preview changes nothing
    assert expect(admin.get(workflow))["active_version"] == 1


def test_previewing_an_edit_that_would_be_refused(admin, workflow, expect):
    config = edited(sample=0.3)
    config["dataset"] = "other"
    result = expect(admin.post(f"{workflow}/preview-edit", {"config": config}))
    assert result["can_activate"] is False
    assert any("new workflow" in error for error in result["preview"]["errors"])
    assert "This edit cannot be activated" in result["text"]


def test_previewing_a_config_that_is_not_valid(admin, workflow, expect):
    result = expect(admin.post(f"{workflow}/preview-edit", {"config": "stages: nope"}))
    assert result["can_activate"] is False
    assert result["preview"]["errors"]


def test_previewing_an_edit_of_an_unknown_workflow(admin, project):
    response = admin.post(f"{project}/workflows/nope/preview-edit", {"config": REVIEW})
    assert response.status_code == 404


def test_making_an_edit(admin, workflow, project, expect):
    body = {"config": edited(sample=0.3), "base_version": 1, "reason": "we need more reviews"}
    result = expect(admin.post(f"{workflow}/edits", body), 201)
    assert result == {"workflow": "xray_main", "config_version": 2, "warnings": []}
    assert expect(admin.get(workflow))["active_version"] == 2
    one, two = expect(admin.get(f"{workflow}/versions"))
    assert (one["active"], two["active"]) == (False, True)
    assert two["reason"] == "we need more reviews"
    assert "-    sample: 0.2" in two["diff"] or "sample: 0.2" in two["diff"]
    assert "sample: 0.3" in two["resolved_yaml"]
    assert two["activated_by"] == "root"


def test_an_edit_can_be_sent_as_yaml_text(admin, workflow, expect):
    text = yaml.safe_dump(edited(sample=0.5), sort_keys=False)
    expect(admin.post(f"{workflow}/edits", {"config": text, "base_version": 1}), 201)
    assert "sample: 0.5" in expect(admin.get(f"{workflow}/versions"))[1]["resolved_yaml"]


def test_an_edit_made_against_an_old_version_is_a_conflict(admin, workflow, expect):
    expect(admin.post(f"{workflow}/edits", {"config": edited(sample=0.3), "base_version": 1}), 201)
    response = admin.post(f"{workflow}/edits", {"config": edited(sample=0.4), "base_version": 1})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "is at config version 2" in detail
    assert "previewed against version 1" in detail
    assert expect(admin.get(workflow))["active_version"] == 2


def test_an_edit_that_cannot_be_applied_is_refused_with_every_reason(admin, workflow):
    config = edited(sample=0.3)
    config["dataset"] = "other"
    config["label_task"] = "other@1"
    response = admin.post(f"{workflow}/edits", {"config": config, "base_version": 1})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"].startswith("The edit was not activated: ")
    assert len(body["problems"]) >= 2
    assert all(problem in body["detail"] for problem in body["problems"])


def test_an_edit_that_changes_nothing_is_refused(admin, workflow):
    response = admin.post(f"{workflow}/edits", {"config": REVIEW, "base_version": 1})
    assert response.status_code == 422
    assert "nothing to activate" in response.json()["detail"]


def test_an_edit_needs_a_base_version(admin, workflow):
    assert admin.post(f"{workflow}/edits", {"config": edited(sample=0.3)}).status_code == 422
    assert (
        admin.post(
            f"{workflow}/edits", {"config": edited(sample=0.3), "base_version": 0}
        ).status_code
        == 422
    )


def test_editing_an_unknown_workflow(admin, project):
    body = {"config": REVIEW, "base_version": 1}
    assert admin.post(f"{project}/workflows/nope/edits", body).status_code == 404
    assert admin.get(f"{project}/workflows/nope/versions").status_code == 404
