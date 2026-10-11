"""Roles: defining them, and granting and revoking them with a reason."""

from __future__ import annotations

TWO_ANNOTATORS = """\
workflow: needs_two
dataset: batch1
label_task: standin@1
stages:
  - name: annotate
    assign_to: annotator
    labels_per_item: 3
"""


def test_roles_are_listed_with_their_holders(admin, project, expect):
    roles = expect(admin.get(f"{project}/roles"))
    assert [(r["role"], r["holders"]) for r in roles] == [
        ("annotator", ["ana", "ben"]),
        ("expert", ["dr_smith"]),
    ]
    assert roles[0]["defined_by"] == "root"
    assert roles[0]["defined_at"] == "2026-10-10T09:00:00Z"


def test_defining_a_role(admin, project, expect):
    created = expect(admin.post(f"{project}/roles", {"role": "trainee"}), 201)
    assert created["role"] == "trainee"
    assert created["holders"] == []
    assert [r["role"] for r in expect(admin.get(f"{project}/roles"))][-1] == "trainee"


def test_defining_a_role_twice_is_a_conflict(admin, project):
    response = admin.post(f"{project}/roles", {"role": "expert"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Role 'expert' is already defined."


def test_a_bad_role_name_is_refused(admin, project):
    response = admin.post(f"{project}/roles", {"role": "2nd reader"})
    assert response.status_code == 422
    assert "not a valid role name" in response.json()["detail"]


def test_granting_a_role(admin, project, new_person, expect, password):
    new_person("cy")
    result = expect(
        admin.post(f"{project}/roles/annotator/grant", {"user": "cy", "reason": "new hire"})
    )
    assert result == {"role": "annotator", "holders": ["ana", "ben", "cy"], "warnings": []}
    cy_projects = expect(admin.post("/auth/login", {"username": "cy", "password": password}))
    assert cy_projects["user"]["user_id"] == "cy"


def test_a_grant_is_logged_with_its_reason(admin, project, new_person, expect):
    new_person("cy")
    admin.post(f"{project}/roles/annotator/grant", {"user": "cy", "reason": "passed the test"})
    events = expect(admin.get(f"{project}/events?type=role_granted"))
    last = events["events"][-1]
    assert (last["user"], last["role"], last["actor"]) == ("cy", "annotator", "root")
    assert last["reason"] == "passed the test"
    assert last["because"] == "granted by root: passed the test"


def test_granting_warns_when_a_running_workflow_is_short_of_people(admin, project, expect):
    expect(admin.post(f"{project}/workflows", {"config": TWO_ANNOTATORS}), 201)
    revoke = admin.post(f"{project}/roles/annotator/revoke", {"user": "ben", "reason": "left"})
    warnings = expect(revoke)["warnings"]
    assert len(warnings) == 1
    assert "needs 3 different people" in warnings[0]
    assert "needs_two" in warnings[0]


def test_a_grant_needs_an_existing_account(admin, project):
    response = admin.post(f"{project}/roles/annotator/grant", {"user": "ghost", "reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == "There is no account named 'ghost'."


def test_a_deactivated_account_cannot_be_given_a_role(admin, ana, project, new_person, expect):
    new_person("cy")
    expect(admin.post("/accounts/cy/deactivate"))
    response = admin.post(f"{project}/roles/annotator/grant", {"user": "cy", "reason": "x"})
    assert response.status_code == 409
    assert "deactivated" in response.json()["detail"]


def test_granting_an_unknown_role_suggests_the_right_one(admin, project):
    response = admin.post(f"{project}/roles/anotator/grant", {"user": "ana", "reason": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == (
        "There is no role named 'anotator'; define it first. Did you mean 'annotator'?"
    )


def test_granting_a_role_twice_is_a_conflict(admin, project):
    response = admin.post(f"{project}/roles/annotator/grant", {"user": "ana", "reason": "again"})
    assert response.status_code == 409
    assert response.json()["detail"] == "ana already holds role 'annotator'."


def test_a_grant_needs_a_reason(admin, project):
    response = admin.post(f"{project}/roles/expert/grant", {"user": "ana"})
    assert response.status_code == 422
    assert "reason: Field required" in response.json()["problems"]
    blank = admin.post(f"{project}/roles/expert/grant", {"user": "ana", "reason": ""})
    assert blank.status_code == 422


def test_revoking_a_role(admin, ana, project, expect):
    result = expect(
        admin.post(f"{project}/roles/annotator/revoke", {"user": "ana", "reason": "moved on"})
    )
    assert result == {"role": "annotator", "holders": ["ben"], "warnings": []}
    assert expect(ana.get("/projects")) == []


def test_revoking_a_role_nobody_holds_is_a_conflict(admin, project):
    response = admin.post(f"{project}/roles/expert/revoke", {"user": "ana", "reason": "x"})
    assert response.status_code == 409
    assert response.json()["detail"] == "ana does not hold role 'expert'."


def test_revoking_an_unknown_role(admin, project):
    response = admin.post(f"{project}/roles/chief/revoke", {"user": "ana", "reason": "x"})
    assert response.status_code == 404


def test_roles_of_an_unknown_project(admin):
    assert admin.get("/projects/nope/roles").status_code == 404
