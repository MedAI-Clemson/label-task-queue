"""Projects: creating them, and who sees which."""

from __future__ import annotations


def test_an_admin_creates_a_project(admin, expect):
    created = expect(admin.post("/projects", {"name": "xray"}), 201)
    assert created == {
        "name": "xray",
        "created_by": "root",
        "created_at": "2026-10-10T09:00:00Z",
        "your_roles": [],
    }


def test_a_duplicate_project_is_a_conflict(admin, expect):
    expect(admin.post("/projects", {"name": "xray"}), 201)
    response = admin.post("/projects", {"name": "xray"})
    assert response.status_code == 409
    assert response.json()["detail"] == "A project named 'xray' already exists."


def test_a_bad_project_name_is_refused(admin):
    response = admin.post("/projects", {"name": "x ray!"})
    assert response.status_code == 422
    assert "not a valid project name" in response.json()["detail"]


def test_a_labeler_cannot_create_a_project(ana):
    assert ana.post("/projects", {"name": "mine"}).status_code == 403


def test_an_admin_sees_every_project(admin, expect):
    for name in ("alpha", "beta"):
        expect(admin.post("/projects", {"name": name}), 201)
    assert [p["name"] for p in expect(admin.get("/projects"))] == ["alpha", "beta"]


def test_a_labeler_sees_the_projects_where_they_hold_a_role(admin, ana, ben, project, expect):
    expect(admin.post("/projects", {"name": "other"}), 201)
    mine = expect(ana.get("/projects"))
    assert [(p["name"], p["your_roles"]) for p in mine] == [("xray", ["annotator"])]
    assert expect(ben.get("/projects"))[0]["your_roles"] == ["annotator"]


def test_a_labeler_without_roles_sees_no_project(admin, new_person, project, expect):
    stranger = new_person("stranger")
    assert expect(stranger.get("/projects")) == []


def test_a_revoked_role_removes_the_project_from_the_list(admin, ana, project, expect):
    revoke = {"user": "ana", "reason": "left the team"}
    expect(admin.post(f"{project}/roles/annotator/revoke", revoke))
    assert expect(ana.get("/projects")) == []


def test_the_list_says_which_roles_an_admin_holds(admin, project, expect):
    expect(admin.post(f"{project}/roles/expert/grant", {"user": "root", "reason": "also reviews"}))
    listed = {p["name"]: p["your_roles"] for p in expect(admin.get("/projects"))}
    assert listed == {"xray": ["expert"]}
