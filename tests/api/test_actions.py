"""Admin actions: exclude an item, cancel the open slots of a stage."""

from __future__ import annotations

import csv
import io

import pytest


def test_excluding_an_item(admin, ana, workflow, expect):
    claimed = expect(ana.post(f"{workflow}/next"))["slot"]  # img_001 is claimed
    result = expect(admin.post(f"{workflow}/items/img_001/exclude", {"reason": "cannot be read"}))
    assert result == {"item": "img_001", "cancelled_slots": [claimed["slot"]]}
    progress = expect(admin.get(workflow))["progress"]
    assert progress["items"]["excluded"] == 1
    assert progress["items"]["in_progress"] == 19
    # ana's claim was cancelled with the item
    assert ana.get(f"{workflow}/slots/{claimed['slot']}").status_code == 403
    assert expect(ana.post(f"{workflow}/next"))["slot"]["item"] == "img_002"


def test_an_exclusion_is_logged_with_its_reason(admin, workflow, project, expect):
    expect(admin.post(f"{workflow}/items/img_003/exclude", {"reason": "cannot be read"}))
    (event,) = expect(admin.get(f"{project}/events?type=item_excluded"))["rows"]
    assert (event["item"], event["actor"], event["reason"]) == ("img_003", "root", "cannot be read")
    assert event["because"] == "excluded by root: cannot be read"


def test_an_item_cannot_be_excluded_twice(admin, workflow, expect):
    expect(admin.post(f"{workflow}/items/img_001/exclude", {"reason": "blurry"}))
    response = admin.post(f"{workflow}/items/img_001/exclude", {"reason": "blurry"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Item img_001 is already excluded from xray_main."


def test_an_item_with_its_final_label_can_be_excluded(
    admin, project, ana, one_stage, label_with, expect
):
    expect(admin.post(f"{project}/workflows", {"config": one_stage("single")}), 201)
    flow = f"{project}/workflows/single"
    label_with(ana, flow, "cat")
    result = expect(admin.post(f"{flow}/items/img_001/exclude", {"reason": "wrong patient"}))
    assert result == {"item": "img_001", "cancelled_slots": []}
    progress = expect(admin.get(flow))["progress"]["items"]
    assert (progress["final"], progress["excluded"]) == (0, 1)
    exported = admin.get(f"{flow}/exports/items?format=csv")
    row = next(csv.DictReader(io.StringIO(exported.text)))
    assert (row["item"], row["status"], row["final_status"]) == ("img_001", "excluded", "final")
    assert row["final_label"] == '{"value":"cat"}'
    explained = expect(admin.get(f"{flow}/items/img_001/explain"))["explanation"]
    assert explained["status"] == "excluded"
    assert explained["final"]["label"] == {"value": "cat"}
    assert [t["type"] for t in explained["trace"]][-2:] == ["item_finalized", "item_excluded"]
    again = admin.post(f"{flow}/items/img_001/exclude", {"reason": "again"})
    assert again.status_code == 409
    assert again.json()["detail"] == "Item img_001 is already excluded from single."


def test_excluding_an_unknown_item(admin, workflow):
    response = admin.post(f"{workflow}/items/nope/exclude", {"reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Item 'nope' is not in workflow 'xray_main'."


def test_an_item_id_may_contain_slashes(admin, ana, project, one_stage, expect, make_items):
    expect(admin.post(f"{project}/datasets", {"name": "nested"}), 201)
    item = make_items(1)[0] | {"item": "patient_07/slice_3.png"}
    expect(admin.post(f"{project}/datasets/nested/items", {"items": [item]}), 201)
    expect(admin.post(f"{project}/workflows", {"config": one_stage("deep", "nested")}), 201)
    flow = f"{project}/workflows/deep"
    explained = expect(admin.get(f"{flow}/items/patient_07/slice_3.png/explain"))
    assert explained["explanation"]["item"] == "patient_07/slice_3.png"
    result = expect(admin.post(f"{flow}/items/patient_07/slice_3.png/exclude", {"reason": "x"}))
    assert result["item"] == "patient_07/slice_3.png"


def test_cancelling_the_open_slots_of_a_stage(admin, ana, workflow, expect):
    claimed = expect(ana.post(f"{workflow}/next"))["slot"]["slot"]
    result = expect(
        admin.post(f"{workflow}/stages/annotate/cancel-open-slots", {"reason": "pause"})
    )
    assert result["stage"] == "annotate"
    assert len(result["cancelled_slots"]) == 19  # the claimed slot is left alone
    assert claimed not in result["cancelled_slots"]
    stage = expect(admin.get(workflow))["progress"]["stages"][0]
    assert (stage["open"], stage["claimed"], stage["cancelled"]) == (0, 1, 19)
    assert expect(ana.get(f"{workflow}/slots/{claimed}"))["slot"]["status"] == "claimed"


def test_cancelling_a_stage_with_no_open_slots_cancels_nothing(admin, workflow, expect):
    result = expect(
        admin.post(f"{workflow}/stages/expert_review/cancel-open-slots", {"reason": "tidy"})
    )
    assert result == {"stage": "expert_review", "cancelled_slots": []}


def test_cancelling_an_unknown_stage(admin, workflow):
    response = admin.post(f"{workflow}/stages/annotat/cancel-open-slots", {"reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Workflow 'xray_main' has no stage named 'annotat'. Did you mean 'annotate'?"
    )


@pytest.mark.parametrize("path", ["items/img_001/exclude", "stages/annotate/cancel-open-slots"])
@pytest.mark.parametrize("body", [{}, {"reason": ""}])
def test_an_action_needs_a_reason(admin, workflow, path, body):
    assert admin.post(f"{workflow}/{path}", body).status_code == 422


@pytest.mark.parametrize("path", ["items/a/exclude", "stages/a/cancel-open-slots"])
def test_actions_on_an_unknown_workflow(admin, project, path):
    assert admin.post(f"{project}/workflows/nope/{path}", {"reason": "x"}).status_code == 404
