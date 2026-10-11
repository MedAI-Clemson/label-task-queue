"""The admin pages: accounts, projects, roles, datasets, workflows, actions, explain."""

from __future__ import annotations

import csv
import io
import json

import pytest

ITEMS_JSON = json.dumps(
    [
        {"item": "img_010", "uri": "xray_512.png"},
        {"item": "img_011", "uri": "xray_512.png", "properties": {"gold_key": {"a": 1}}},
    ]
)
ITEMS_CSV = "item,uri,content_hash,site\nimg_020,xray_512.png,,clemson\nimg_021,xray_512.png,,\n"


# --- who may see them -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/ui/admin/accounts",
        "/ui/admin/projects",
        "/ui/admin/projects/xray",
        "/ui/admin/projects/xray/workflows/new",
        "/ui/admin/projects/xray/workflows/xray_main",
    ],
)
def test_admin_pages_need_an_administrator(path, anon, ana, workflow):
    assert anon.get(path).status_code == 303  # to the login page
    response = ana.get(path)
    assert response.status_code == 403
    assert "This needs an administrator account." in response.text


def test_an_htmx_request_from_a_stranger_gets_a_redirect_header(anon, workflow):
    response = anon.post(
        "/ui/admin/projects/xray/workflows/validate",
        {"config": "x"},
        csrf=False,
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"].startswith("/ui/login?next=")


# --- accounts -------------------------------------------------------------------------------------


def test_accounts_page_and_forms(admin, ana, expect, app):
    page = expect(admin.get("/ui/admin/accounts"))
    assert "<h1>Accounts</h1>" in page
    assert "ana" in page and "root" in page
    # create
    form = {"username": "carl", "password": "carlspassword", "display_name": "Carl"}
    response = admin.post("/ui/admin/accounts", form)
    assert response.status_code == 303
    page = expect(admin.get("/ui/admin/accounts"))
    assert "Created the account &#39;carl&#39;." in page
    assert "<td>Carl</td>" in page
    # the same username again, and a bad one
    response = admin.post("/ui/admin/accounts", form)
    assert response.status_code == 409
    assert "already exists" in response.text
    response = admin.post("/ui/admin/accounts", {"username": "Bad Name", "password": "x" * 8})
    assert response.status_code == 422
    assert "not a valid username" in response.text
    # reset password: carl can log in with the new one
    response = admin.post("/ui/admin/accounts/carl/password", {"password": "anothersecret"})
    assert response.status_code == 303
    assert "Set a new password" in expect(admin.get("/ui/admin/accounts"))
    assert (
        ana.client.post(
            "/ui/login", data={"username": "carl", "password": "anothersecret"}
        ).status_code
        == 303
    )
    assert (
        admin.post("/ui/admin/accounts/nobody/password", {"password": "x" * 9}).status_code == 404
    )
    # deactivate: not yourself, not twice
    response = admin.post("/ui/admin/accounts/root/deactivate")
    assert response.status_code == 409
    assert "your own account" in response.text
    assert admin.post("/ui/admin/accounts/carl/deactivate").status_code == 303
    page = expect(admin.get("/ui/admin/accounts"))
    assert "Deactivated the account" in page
    assert app.state.runtime.accounts.get("carl").active is False
    assert admin.post("/ui/admin/accounts/nobody/deactivate").status_code == 404


def test_a_deactivated_person_is_logged_out(admin, ana, app):
    assert admin.post("/ui/admin/accounts/ana/deactivate").status_code == 303
    assert ana.get("/ui/").status_code == 303  # back to the login page


# --- projects, roles, datasets ------------------------------------------------------------------


def test_projects_page_and_creating_one(admin, expect):
    page = expect(admin.get("/ui/admin/projects"))
    assert "No project yet." in page
    response = admin.post("/ui/admin/projects", {"name": "9bad"})
    assert response.status_code == 422
    assert "not a valid project name" in response.text
    response = admin.post("/ui/admin/projects", {"name": "pilot"})
    assert response.headers["location"] == "/ui/admin/projects/pilot"
    page = expect(admin.get("/ui/admin/projects/pilot"))
    assert "Created project" in page
    assert "<h1>Project pilot</h1>" in page
    assert "No role defined yet." in page
    assert admin.post("/ui/admin/projects", {"name": "pilot"}).status_code == 409
    assert admin.get("/ui/admin/projects/nope").status_code == 404


def test_roles_define_grant_and_revoke_with_staffing_warnings(admin, workflow, expect):
    page = expect(admin.get("/ui/admin/projects/xray"))
    assert "annotator" in page and "ana, ben" in page and "dr_smith" in page
    assert admin.post("/ui/admin/projects/xray/roles", {"role": "trainee"}).status_code == 303
    page = expect(admin.get("/ui/admin/projects/xray"))
    assert "Defined the role &#39;trainee&#39;." in page
    assert admin.post("/ui/admin/projects/xray/roles", {"role": "trainee"}).status_code == 409
    assert admin.post("/ui/admin/projects/xray/roles", {"role": "9x"}).status_code == 422
    # grant needs an existing, active account
    response = admin.post(
        "/ui/admin/projects/xray/roles/trainee/grant", {"user": "nobody", "reason": "x"}
    )
    assert response.status_code == 404
    response = admin.post(
        "/ui/admin/projects/xray/roles/trainee/grant", {"user": "ana", "reason": "learning"}
    )
    assert response.status_code == 303
    page = expect(admin.get("/ui/admin/projects/xray"))
    assert "Gave the role &#39;trainee&#39; to ana." in page
    # revoking the only expert warns that the review stage cannot be filled
    response = admin.post(
        "/ui/admin/projects/xray/roles/expert/revoke", {"user": "dr_smith", "reason": "on leave"}
    )
    assert response.status_code == 303
    page = expect(admin.get("/ui/admin/projects/xray"))
    assert "Took the role &#39;expert&#39; from dr_smith." in page
    assert "Warning:" in page and "expert_review" in page
    assert "Staffing warnings:" in page
    response = admin.post(
        "/ui/admin/projects/xray/roles/expert/revoke", {"user": "dr_smith", "reason": "again"}
    )
    assert response.status_code == 409


def test_datasets_create_and_import(admin, project, expect, app):
    assert admin.post("/ui/admin/projects/xray/datasets", {"name": "batch1"}).status_code == 409
    response = admin.post("/ui/admin/projects/xray/datasets", {"name": "batch2"})
    assert response.headers["location"] == "/ui/admin/projects/xray/datasets/batch2"
    page = expect(admin.get("/ui/admin/projects/xray/datasets/batch2"))
    assert "Created dataset" in page and "No item yet." in page
    assert admin.get("/ui/admin/projects/xray/datasets/nope").status_code == 404
    # JSON, hashed by the server from the media folder
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": ITEMS_JSON})
    assert response.status_code == 303, response.text
    page = expect(admin.get("/ui/admin/projects/xray/datasets/batch2"))
    assert "Imported 2 items." in page
    assert "img_010" in page and "gold_key" in page
    # CSV, with an extra column as a property
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": ITEMS_CSV})
    assert response.status_code == 303, response.text
    engine = app.state.runtime.engine_for("xray")
    items = {row.item: row for row in engine.items("batch2", 0, 10).rows}
    assert set(items) == {"img_010", "img_011", "img_020", "img_021"}
    assert items["img_020"].properties == {"site": "clemson"}
    assert items["img_021"].properties == {}
    assert len({row.content_hash for row in items.values()}) == 1
    # problems are listed, and nothing is imported
    bad = json.dumps(
        [
            {"item": "img_010", "uri": "xray_512.png"},
            {"item": "nofile", "uri": "missing.png"},
            {"uri": "x"},
        ]
    )
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": bad})
    assert response.status_code == 422
    assert "No items were imported: 2 of 3 have problems." in response.text
    assert "missing.png" in response.text
    assert "Item 3 (item): Field required." in response.text
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": ""})
    assert response.status_code == 422
    assert "Paste the items" in response.text
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": "{bad"})
    assert "not valid JSON" in response.text
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": "item,uri\n"})
    assert "no rows" in response.text
    duplicate = json.dumps([{"item": "img_010", "uri": "xray_512.png"}])
    response = admin.post("/ui/admin/projects/xray/datasets/batch2/items", {"items": duplicate})
    assert response.status_code == 409
    assert engine.dataset("batch2").items == 4


# --- workflows ------------------------------------------------------------------------------------


def test_the_new_workflow_page_checks_the_config_as_you_type(admin, project, expect, review_config):
    page = expect(admin.get("/ui/admin/projects/xray/workflows/new"))
    assert "hx-post" in page and "/workflows/validate" in page
    assert "mermaid" in page
    fragment = expect(admin.post("/ui/admin/projects/xray/workflows/validate", {"config": ""}))
    assert "Paste or type a config" in fragment
    bad = (
        "workflow: w\ndataset: nope\nlabel_task: image_ontology@1\n"
        "stages:\n  - name: a\n    assign_to: ghost\n"
    )
    fragment = expect(admin.post("/ui/admin/projects/xray/workflows/validate", {"config": bad}))
    assert "The config has 2 errors." in fragment
    assert "nope" in fragment and "ghost" in fragment
    assert '<pre class="mermaid">' not in fragment
    fragment = expect(
        admin.post("/ui/admin/projects/xray/workflows/validate", {"config": review_config})
    )
    assert "The config is valid." in fragment
    assert '<pre class="mermaid">flowchart' in fragment
    assert "sample: 0.2" in fragment and "labels_per_item: 1" in fragment  # resolved YAML


def test_starting_a_workflow(admin, project, expect, app, review_config):
    bad = "workflow: w\n"
    response = admin.post("/ui/admin/projects/xray/workflows", {"config": bad})
    assert response.status_code == 422
    assert "error" in response.text
    response = admin.post(
        "/ui/admin/projects/xray/workflows", {"config": review_config, "seed": "abc"}
    )
    assert response.status_code == 422
    assert "The seed must be a whole number" in response.text
    response = admin.post(
        "/ui/admin/projects/xray/workflows", {"config": review_config, "seed": "11"}
    )
    assert response.headers["location"] == "/ui/admin/projects/xray/workflows/xray_main"
    page = expect(admin.get("/ui/admin/projects/xray/workflows/xray_main"))
    assert "Started workflow &#39;xray_main&#39; with seed 11." in page
    assert app.state.runtime.engine_for("xray").workflow("xray_main").seed == 11
    response = admin.post("/ui/admin/projects/xray/workflows", {"config": review_config})
    assert response.status_code == 409
    assert "already exists" in response.text


def test_the_workflow_page_shows_progress_diagram_and_exports(admin, workflow, expect):
    page = expect(admin.get("/ui/admin/projects/xray/workflows/xray_main"))
    assert "<h1>Workflow xray_main</h1>" in page
    assert "<td>3</td>" in page  # entered
    assert "annotate" in page and "expert_review" in page
    assert '<pre class="mermaid">flowchart' in page
    assert "sample: 0.2" in page
    assert "/projects/xray/workflows/xray_main/exports/items?format=csv" in page
    assert "/projects/xray/workflows/xray_main/exports/labels?format=parquet" in page
    assert "Version 1" in page and "active" in page
    assert admin.get("/ui/admin/projects/xray/workflows/nope").status_code == 404
    # the export links work with the login cookie
    response = admin.get("/projects/xray/workflows/xray_main/exports/items?format=csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [r["item"] for r in rows] == ["img_001", "img_002", "img_003"]


def test_pausing_and_resuming(admin, ana, workflow, expect):
    base = "/ui/admin/projects/xray/workflows/xray_main"
    assert admin.post(f"{base}/pause", {"reason": "scanner recalibration"}).status_code == 303
    page = expect(admin.get(base))
    assert "Paused workflow" in page
    assert "Paused by root" in page and "scanner recalibration" in page
    assert "Resume workflow" in page
    response = admin.post(f"{base}/pause", {"reason": "again"})
    assert response.status_code == 409
    assert "already paused" in response.text
    assert "No work for you" in ana.post("/ui/projects/xray/workflows/xray_main/next").text
    assert admin.post(f"{base}/resume", {"reason": "done"}).status_code == 303
    assert admin.post(f"{base}/resume", {"reason": "done"}).status_code == 409
    assert admin.post(f"{base}/stages/annotate/pause", {"reason": "wait"}).status_code == 303
    page = expect(admin.get(base))
    assert "Paused stage" in page and "Resume stage" in page
    assert admin.post(f"{base}/stages/nope/pause", {"reason": "wait"}).status_code == 404
    assert admin.post(f"{base}/stages/annotate/resume", {"reason": "go"}).status_code == 303
    assert "Resumed stage" in expect(admin.get(base))


def test_cancel_open_slots_and_exclude(admin, workflow, expect, app):
    base = "/ui/admin/projects/xray/workflows/xray_main"
    response = admin.post(f"{base}/items/exclude", {"item": "img_003", "reason": "unreadable"})
    assert response.status_code == 303
    page = expect(admin.get(base))
    assert "Excluded item &#39;img_003&#39;" in page
    response = admin.post(f"{base}/items/exclude", {"item": "img_003", "reason": "again"})
    assert response.status_code == 409
    assert admin.post(f"{base}/items/exclude", {"item": "nope", "reason": "x"}).status_code == 404
    response = admin.post(f"{base}/stages/annotate/cancel-open-slots", {"reason": "restart"})
    assert response.status_code == 303
    assert "Cancelled 2 open slots" in expect(admin.get(base))
    progress = app.state.runtime.engine_for("xray").progress("xray_main")
    assert progress.items.excluded == 1


def test_explain(admin, workflow, expect):
    base = "/ui/admin/projects/xray/workflows/xray_main"
    page = expect(admin.get(f"{base}/explain"))
    assert "<h1>Explain an item</h1>" in page
    page = expect(admin.get(f"{base}/explain?item=img_001"))
    assert "img_001" in page
    assert "in_progress" in page
    assert "item_entered" in page or "entered" in page
    assert "the item was in the dataset when the workflow started" in page
    assert admin.get(f"{base}/explain?item=nope").status_code == 404


def test_preview_and_activate_an_edit(admin, workflow, expect, app, review_config):
    base = "/ui/admin/projects/xray/workflows/xray_main"
    fragment = expect(admin.post(f"{base}/preview-edit", {"config": ""}))
    assert "Paste the whole new config" in fragment
    edited = review_config.replace("sample: 0.2", "sample: 0.5")
    fragment = expect(admin.post(f"{base}/preview-edit", {"config": edited}))
    assert "This edit can be activated as version 2." in fragment
    assert "-    sample: 0.2" in fragment and "+    sample: 0.5" in fragment
    refused = review_config.replace("dataset: batch1", "dataset: batch2")
    response = admin.post(f"{base}/preview-edit", {"config": refused})
    assert response.status_code in (200, 404, 422)
    assert "cannot be activated" in response.text or "batch2" in response.text
    # activating: a wrong base version, a refused edit, then the real one
    response = admin.post(f"{base}/edits", {"config": edited, "base_version": "x"})
    assert response.status_code == 422
    response = admin.post(f"{base}/edits", {"config": edited, "base_version": "7"})
    assert response.status_code == 409
    assert "config version 1" in response.text
    response = admin.post(
        f"{base}/edits", {"config": edited, "base_version": "1", "reason": "more review"}
    )
    assert response.headers["location"] == base
    page = expect(admin.get(base))
    assert "Activated config version 2" in page
    assert "Version 2" in page and "more review" in page
    assert "+    sample: 0.5" in page
    detail = app.state.runtime.engine_for("xray").workflow("xray_main")
    assert detail.active_version == 2
