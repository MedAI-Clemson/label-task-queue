"""The labeler's calls: ask for work, look at it, submit, release, claim."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

REVIEW_CHECKED = {
    "workflow": "checked",
    "dataset": "batch1",
    "label_task": "standin@1",
    "stages": [
        {"name": "annotate", "assign_to": "annotator"},
        {"name": "expert_review", "input": "annotate", "assign_to": "expert"},
    ],
    "final": ["expert_review", "annotate"],
}


def test_asking_for_work_claims_the_first_slot(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    assert work["slot"]["status"] == "claimed"
    assert work["slot"]["claimed_by"] == "ana"
    assert work["slot"]["stage"] == "annotate"
    assert work["slot"]["item"] == "img_001"  # the oldest item first
    packet = work["packet"]
    assert packet["item"] == "img_001"
    assert packet["uri"] == "https://images.example.org/img_001.png"
    assert len(packet["content_hash"]) == 64
    assert packet["prior_labels"] == []
    assert work["shown"] == {
        "content_hash": packet["content_hash"],
        "prior_label_ids": [],
        "show_prior": True,
        "consensus_rule_version": None,
    }


def test_asking_again_returns_the_same_slot(ana, workflow, expect):
    first = expect(ana.post(f"{workflow}/next"))
    again = expect(ana.post(f"{workflow}/next"))
    assert first == again


def test_two_people_get_different_slots(ana, ben, workflow, expect):
    first = expect(ana.post(f"{workflow}/next"))["slot"]
    second = expect(ben.post(f"{workflow}/next"))["slot"]
    assert first["slot"] != second["slot"]
    assert first["item"] != second["item"]


def test_there_is_no_work_for_someone_without_a_matching_role(dr_smith, workflow):
    response = dr_smith.post(f"{workflow}/next")
    assert response.status_code == 204
    assert response.content == b""


def test_there_is_no_work_for_a_stranger(new_person, workflow):
    assert new_person("stranger").post(f"{workflow}/next").status_code == 204


def test_asking_for_work_in_an_unknown_workflow(ana, project):
    response = ana.post(f"{project}/workflows/nope/next")
    assert response.status_code == 404
    assert response.json()["detail"] == "There is no workflow named 'nope'."


def test_asking_for_work_in_an_unknown_project(ana, project):
    assert ana.post("/projects/nope/workflows/xray_main/next").status_code == 404


def test_the_work_of_a_held_slot_can_be_shown_again(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    slot = work["slot"]["slot"]
    assert expect(ana.get(f"{workflow}/slots/{slot}")) == work


def test_the_work_of_someone_elses_slot_cannot_be_seen(ana, ben, workflow, expect):
    slot = expect(ana.post(f"{workflow}/next"))["slot"]["slot"]
    response = ben.get(f"{workflow}/slots/{slot}")
    assert response.status_code == 403
    assert response.json()["detail"] == f"Slot {slot} is not claimed by ben: it is claimed by ana."


def test_the_work_of_an_unknown_slot(ana, workflow):
    response = ana.get(f"{workflow}/slots/9999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow 'xray_main' has no slot 9999."


def test_item_properties_are_never_shown_to_a_labeler(admin, ana, project, make_items, expect):
    expect(admin.post(f"{project}/datasets", {"name": "gold"}), 201)
    items = make_items(2, gold_key="secret-answer")
    expect(admin.post(f"{project}/datasets/gold/items", {"items": items}), 201)
    config = REVIEW_CHECKED | {"workflow": "gold_round", "dataset": "gold"}
    expect(admin.post(f"{project}/workflows", {"config": config}), 201)
    work = expect(ana.post(f"{project}/workflows/gold_round/next"))
    assert "secret-answer" not in json.dumps(work)


def test_submitting_a_label(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    slot = work["slot"]["slot"]
    body = {"label": {"value": "cat"}, "shown": work["shown"], "duration_ms": 4200}
    assert expect(ana.post(f"{workflow}/slots/{slot}/submit", body), 201) == {"label_id": 1}
    # the slot is filled, and the next request gives a new one
    nxt = expect(ana.post(f"{workflow}/next"))
    assert nxt["slot"]["item"] == "img_002"


def test_a_submitted_label_is_recorded_with_what_was_shown(admin, ana, workflow, project, expect):
    work = expect(ana.post(f"{workflow}/next"))
    body = {"label": {"value": "cat"}, "shown": work["shown"], "duration_ms": 4200}
    expect(ana.post(f"{workflow}/slots/{work['slot']['slot']}/submit", body), 201)
    (event,) = expect(admin.get(f"{project}/events?type=label_submitted"))["events"]
    assert event["user"] == "ana"
    assert event["roles"] == ["annotator"]
    assert event["duration_ms"] == 4200
    assert event["shown"]["content_hash"] == work["packet"]["content_hash"]
    assert event["label"] == {"value": "cat"}


def test_the_duration_is_optional(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    body = {"label": {"value": "cat"}, "shown": work["shown"]}
    expect(ana.post(f"{workflow}/slots/{work['slot']['slot']}/submit", body), 201)


def test_a_label_that_does_not_fit_is_rejected_and_the_slot_stays_held(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    slot = work["slot"]["slot"]
    response = ana.post(
        f"{workflow}/slots/{slot}/submit",
        {"label": {"value": "", "colour": "red"}, "shown": work["shown"]},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"].startswith("The label was rejected: ")
    assert sorted(body["problems"]) == [
        "The field 'value' must be a non-empty string.",
        "Unknown field 'colour'.",
    ]
    assert expect(ana.get(f"{workflow}/slots/{slot}"))["slot"]["status"] == "claimed"


def test_what_was_shown_must_match_the_item(ana, workflow):
    work = ana.post(f"{workflow}/next").json()
    shown = work["shown"] | {"content_hash": "0" * 64}
    response = ana.post(
        f"{workflow}/slots/{work['slot']['slot']}/submit",
        {"label": {"value": "cat"}, "shown": shown},
    )
    assert response.status_code == 422
    assert "reports showing content" in response.json()["detail"]


@pytest.mark.parametrize(
    "mistake",
    [
        {"duration_ms": -5},
        {"duration_ms": "slow"},
        {"label": "cat"},
        {"label": None},
        {"shown": {"content_hash": "x"}},
        {"shown": None},
    ],
)
def test_a_malformed_submission_is_refused(ana, workflow, mistake):
    work = ana.post(f"{workflow}/next").json()
    body = {"label": {"value": "cat"}, "shown": work["shown"]} | mistake
    response = ana.post(f"{workflow}/slots/{work['slot']['slot']}/submit", body)
    assert response.status_code == 422, mistake
    assert response.json()["detail"].startswith("The request is not valid")


def test_shown_cannot_carry_extra_keys(ana, workflow):
    work = ana.post(f"{workflow}/next").json()
    shown = work["shown"] | {"extra": 1}
    response = ana.post(
        f"{workflow}/slots/{work['slot']['slot']}/submit",
        {"label": {"value": "cat"}, "shown": shown},
    )
    assert response.status_code == 422


def test_a_slot_that_is_not_yours_cannot_be_submitted(ana, ben, workflow):
    work = ana.post(f"{workflow}/next").json()
    body = {"label": {"value": "cat"}, "shown": work["shown"]}
    response = ben.post(f"{workflow}/slots/{work['slot']['slot']}/submit", body)
    assert response.status_code == 403
    assert "is not claimed by ben" in response.json()["detail"]


def test_a_slot_cannot_be_submitted_twice(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    slot = work["slot"]["slot"]
    body = {"label": {"value": "cat"}, "shown": work["shown"]}
    expect(ana.post(f"{workflow}/slots/{slot}/submit", body), 201)
    again = ana.post(f"{workflow}/slots/{slot}/submit", body)
    assert again.status_code == 403
    assert "it already has its label" in again.json()["detail"]


def test_submitting_to_an_unknown_slot(ana, workflow):
    body = {"label": {"value": "cat"}, "shown": {"content_hash": "x", "show_prior": True}}
    assert ana.post(f"{workflow}/slots/9999/submit", body).status_code == 404


def test_releasing_a_slot(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    slot = work["slot"]["slot"]
    released = expect(ana.post(f"{workflow}/slots/{slot}/release", {"reason": "unreadable image"}))
    assert released["slot"] == slot
    assert released["status"] == "open"
    assert released["claimed_by"] is None
    # ana is offered the same item again only after her other work, so she moves on
    assert expect(ana.post(f"{workflow}/next"))["slot"]["item"] == "img_002"


def test_a_released_slot_can_be_taken_by_someone_else(ana, ben, workflow, expect):
    slot = expect(ana.post(f"{workflow}/next"))["slot"]["slot"]
    expect(ana.post(f"{workflow}/slots/{slot}/release", {"reason": "unreadable image"}))
    assert expect(ben.post(f"{workflow}/next"))["slot"]["slot"] == slot


def test_releasing_needs_a_reason(ana, workflow):
    slot = ana.post(f"{workflow}/next").json()["slot"]["slot"]
    assert ana.post(f"{workflow}/slots/{slot}/release", {}).status_code == 422
    assert ana.post(f"{workflow}/slots/{slot}/release", {"reason": ""}).status_code == 422
    assert ana.post(f"{workflow}/slots/{slot}/release", {"reason": "  "}).status_code == 422


def test_releasing_someone_elses_slot(ana, ben, workflow):
    slot = ana.post(f"{workflow}/next").json()["slot"]["slot"]
    response = ben.post(f"{workflow}/slots/{slot}/release", {"reason": "mine now"})
    assert response.status_code == 403


def test_claiming_a_particular_slot(ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/slots/5/claim"))
    assert work["slot"]["slot"] == 5
    assert work["slot"]["claimed_by"] == "ana"
    assert work["packet"]["item"] == "img_005"


def test_you_cannot_claim_a_slot_while_holding_another(ana, workflow, expect):
    expect(ana.post(f"{workflow}/next"))
    response = ana.post(f"{workflow}/slots/5/claim")
    assert response.status_code == 409
    assert "already holds slot 1" in response.json()["detail"]


def test_you_cannot_claim_a_slot_that_is_taken(ana, ben, workflow, expect):
    expect(ben.post(f"{workflow}/slots/5/claim"))
    response = ana.post(f"{workflow}/slots/5/claim")
    assert response.status_code == 409
    assert response.json()["detail"] == "Slot 5 is not open: it is claimed."


def test_you_cannot_claim_a_slot_you_are_not_eligible_for(dr_smith, workflow):
    response = dr_smith.post(f"{workflow}/slots/5/claim")
    assert response.status_code == 403
    assert response.json()["detail"].startswith("dr_smith may not claim slot 5: ")


def test_you_may_claim_an_item_you_released_again(ana, workflow, expect):
    slot = expect(ana.post(f"{workflow}/slots/5/claim"))["slot"]["slot"]
    expect(ana.post(f"{workflow}/slots/{slot}/release", {"reason": "unreadable"}))
    again = expect(ana.post(f"{workflow}/slots/{slot}/claim"))
    assert (again["slot"]["slot"], again["slot"]["claimed_by"]) == (slot, "ana")


def test_an_item_you_released_is_offered_to_you_last(ana, workflow, label_with, expect):
    first = expect(ana.post(f"{workflow}/next"))["slot"]
    assert first["item"] == "img_001"
    expect(ana.post(f"{workflow}/slots/{first['slot']}/release", {"reason": "later"}))
    offered = []
    while (work := label_with(ana, workflow)) is not None:
        offered.append(work["slot"]["item"])
    assert offered == [f"img_{n:03d}" for n in range(2, 21)] + ["img_001"]


def test_claiming_an_unknown_slot(ana, workflow):
    assert ana.post(f"{workflow}/slots/9999/claim").status_code == 404


def test_a_reviewer_sees_the_earlier_label(admin, project, ana, dr_smith, label_with, expect):
    expect(admin.post(f"{project}/workflows", {"config": REVIEW_CHECKED}), 201)
    flow = f"{project}/workflows/checked"
    label_with(ana, flow, "cat")
    work = expect(dr_smith.post(f"{flow}/next"))
    assert work["slot"]["stage"] == "expert_review"
    (prior,) = work["packet"]["prior_labels"]
    assert prior["stage"] == "annotate"
    assert prior["value"] == {"value": "cat"}
    assert "ana" not in json.dumps(work)  # earlier labelers are not named
    assert work["shown"]["prior_label_ids"] == [prior["label"]]
    body = {"label": {"value": "dog"}, "shown": work["shown"]}
    expect(dr_smith.post(f"{flow}/slots/{work['slot']['slot']}/submit", body), 201)


def test_a_reviewer_may_report_less_than_the_packet_held(
    admin, project, ana, dr_smith, label_with, expect
):
    expect(admin.post(f"{project}/workflows", {"config": REVIEW_CHECKED}), 201)
    flow = f"{project}/workflows/checked"
    label_with(ana, flow, "cat")
    work = expect(dr_smith.post(f"{flow}/next"))
    shown = work["shown"] | {"prior_label_ids": [], "show_prior": False}
    body = {"label": {"value": "dog"}, "shown": shown}
    expect(dr_smith.post(f"{flow}/slots/{work['slot']['slot']}/submit", body), 201)


def test_later_stages_are_offered_first(admin, project, ana, ben, label_with, expect):
    expect(admin.post(f"{project}/roles/expert/grant", {"user": "ben", "reason": "both"}))
    expect(admin.post(f"{project}/workflows", {"config": REVIEW_CHECKED}), 201)
    flow = f"{project}/workflows/checked"
    label_with(ana, flow, "cat")  # img_001 now waits for a review
    work = expect(ben.post(f"{flow}/next"))
    assert (work["slot"]["stage"], work["slot"]["item"]) == ("expert_review", "img_001")
    # ana labeled img_001, so she cannot review it: she is offered the next annotation
    other = expect(ana.post(f"{flow}/next"))
    assert (other["slot"]["stage"], other["slot"]["item"]) == ("annotate", "img_002")


def test_labelers_working_at_the_same_time_never_share_a_slot(
    admin, project, new_person, label_with, expect
):
    people = [new_person(name) for name in ("cy", "di", "ed", "flo")]
    for person in people:
        expect(admin.post(f"{project}/roles/annotator/grant", {"user": person.name, "reason": "x"}))
    config = {
        "workflow": "rush",
        "dataset": "batch1",
        "label_task": "standin@1",
        "stages": [{"name": "annotate", "assign_to": "annotator"}],
    }
    expect(admin.post(f"{project}/workflows", {"config": config}), 201)
    flow = f"{project}/workflows/rush"

    def work_until_done(person):
        slots = []
        while (work := label_with(person, flow, "cat")) is not None:
            slots.append(work["slot"]["slot"])
        return slots

    with ThreadPoolExecutor(max_workers=len(people)) as pool:
        results = list(pool.map(work_until_done, people))

    claimed = [slot for slots in results for slot in slots]
    assert sorted(claimed) == list(range(1, 21))  # every slot labeled once, none twice
    assert expect(admin.get(flow))["progress"]["items"]["final"] == 20
