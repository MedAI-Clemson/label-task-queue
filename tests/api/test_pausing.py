"""Pausing and resuming claims in a stage or a whole workflow."""

from __future__ import annotations


def test_pausing_a_stage(admin, ana, workflow, expect):
    state = expect(admin.post(f"{workflow}/stages/annotate/pause", {"reason": "new guidelines"}))
    assert (state["workflow"], state["paused"], state["pause"]) == ("xray_main", False, None)
    annotate, review = state["stages"]
    assert (annotate["stage"], annotate["paused"]) == ("annotate", True)
    assert annotate["pause"] == {
        "by": "root",
        "since": "2026-10-10T09:00:00Z",
        "reason": "new guidelines",
    }
    assert (review["stage"], review["paused"], review["pause"]) == ("expert_review", False, None)
    # Everything ana could do is paused: no work, and a claim is refused.
    assert ana.post(f"{workflow}/next").status_code == 204
    response = ana.post(f"{workflow}/slots/1/claim")
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Slot 1 cannot be claimed now: stage annotate is paused (by root since 2026-10-10 "
        "09:00 UTC: new guidelines). It can be claimed once that is resumed."
    )
    resumed = expect(admin.post(f"{workflow}/stages/annotate/resume", {"reason": "published"}))
    assert resumed["stages"][0] == {"stage": "annotate", "paused": False, "pause": None}
    assert expect(ana.post(f"{workflow}/next"))["slot"]["stage"] == "annotate"


def test_pausing_a_whole_workflow(admin, ana, workflow, project, expect):
    state = expect(admin.post(f"{workflow}/pause", {"reason": "data audit"}))
    assert state["paused"] is True and state["pause"]["reason"] == "data audit"
    assert [s["paused"] for s in state["stages"]] == [False, False]
    assert ana.post(f"{workflow}/next").status_code == 204
    assert ana.post(f"{workflow}/slots/1/claim").status_code == 409
    (event,) = expect(admin.get(f"{project}/events?type=workflow_paused"))["events"]
    assert (event["actor"], event["reason"]) == ("root", "data audit")
    state = expect(admin.post(f"{workflow}/resume", {"reason": "audit done"}))
    assert (state["paused"], state["pause"]) == (False, None)
    assert expect(ana.post(f"{workflow}/next"))["slot"]["item"] == "img_001"


def test_the_workflow_detail_shows_what_is_paused(admin, ana, workflow, expect):
    detail = expect(admin.get(workflow))
    assert (detail["paused"], detail["pause"]) == (False, None)
    assert [(s["name"], s["paused"]) for s in detail["stages"]] == [
        ("annotate", False),
        ("expert_review", False),
    ]
    expect(admin.post(f"{workflow}/stages/expert_review/pause", {"reason": "experts away"}))
    expect(admin.post(f"{workflow}/pause", {"reason": "audit"}))
    detail = expect(ana.get(workflow))  # labelers see it too: it says why there is no work
    assert detail["paused"] is True and detail["pause"]["by"] == "root"
    review = detail["stages"][1]
    assert review["assign_to"] == ["expert"]
    assert review["paused"] is True and review["pause"]["reason"] == "experts away"
    # Resuming the workflow leaves the stage paused.
    expect(admin.post(f"{workflow}/resume", {"reason": "done"}))
    detail = expect(admin.get(workflow))
    assert detail["paused"] is False and detail["stages"][1]["paused"] is True


def test_a_held_claim_can_be_submitted_while_paused(admin, ana, workflow, expect):
    work = expect(ana.post(f"{workflow}/next"))
    expect(admin.post(f"{workflow}/pause", {"reason": "audit"}))
    held = expect(ana.post(f"{workflow}/next"))  # the claim she holds comes back
    assert held["slot"]["slot"] == work["slot"]["slot"]
    body = {"label": {"value": "cat"}, "shown": work["shown"]}
    expect(ana.post(f"{workflow}/slots/{work['slot']['slot']}/submit", body), 201)
    assert ana.post(f"{workflow}/next").status_code == 204


def test_pausing_twice_or_resuming_what_is_not_paused_is_a_conflict(admin, workflow, expect):
    expect(admin.post(f"{workflow}/pause", {"reason": "audit"}))
    again = admin.post(f"{workflow}/pause", {"reason": "audit"})
    assert again.status_code == 409
    assert again.json()["detail"].startswith(
        "The workflow xray_main is already paused (by root since 2026-10-10 09:00 UTC: audit)"
    )
    response = admin.post(f"{workflow}/stages/annotate/resume", {"reason": "go"})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The stage annotate of xray_main is not paused, so there is nothing to resume."
    )
    expect(admin.post(f"{workflow}/stages/annotate/pause", {"reason": "hold"}))
    assert admin.post(f"{workflow}/stages/annotate/pause", {"reason": "x"}).status_code == 409
    expect(admin.post(f"{workflow}/resume", {"reason": "done"}))
    assert admin.post(f"{workflow}/resume", {"reason": "done"}).status_code == 409


def test_pausing_needs_a_reason_an_admin_and_a_known_stage(admin, ana, workflow, project):
    for path in ("pause", "resume", "stages/annotate/pause", "stages/annotate/resume"):
        assert admin.post(f"{workflow}/{path}", {}).status_code == 422
        assert admin.post(f"{workflow}/{path}", {"reason": ""}).status_code == 422
        assert ana.post(f"{workflow}/{path}", {"reason": "x"}).status_code == 403
        assert admin.post(f"{project}/workflows/nope/{path}", {"reason": "x"}).status_code == 404
    response = admin.post(f"{workflow}/stages/annotat/pause", {"reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"].endswith("Did you mean 'annotate'?")
    assert admin.post(f"{workflow}/stages/annotat/resume", {"reason": "x"}).status_code == 404
