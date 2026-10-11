"""The login, home and labeler pages, logged in and out."""

from __future__ import annotations

import json
import re

from labelflow.engine import NewItem, Shown
from labelflow.engine.config import validate_config

# --- login and logout ---------------------------------------------------------------------------


def test_the_root_and_the_home_page_send_a_stranger_to_the_login_page(anon, expect):
    assert anon.get("/").headers["location"] == "/ui/"
    response = anon.get("/ui/")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?next=/ui/"
    page = expect(anon.get("/ui/login?next=/ui/"))
    assert "<h1>Log in</h1>" in page
    assert 'name="next" value="/ui/"' in page


def test_a_wrong_password_is_refused_on_the_page(anon, admin, expect):
    response = anon.login("root", "not-the-password")
    assert response.status_code == 401
    assert "Wrong username or password." in response.text
    assert "set-cookie" not in response.headers
    assert anon.login("", "").status_code == 401


def test_logging_in_sets_the_cookie_and_goes_to_the_next_page(app, password, new_person):
    person = new_person("ana")
    assert "labelflow_session" in person.client.cookies
    # the next parameter must be a path on this site
    response = person.client.post(
        "/ui/login", data={"username": "ana", "password": password, "next": "//evil.example"}
    )
    assert response.headers["location"] == "/ui/"
    response = person.client.post(
        "/ui/login", data={"username": "ana", "password": password, "next": "/ui/admin/accounts"}
    )
    assert response.headers["location"] == "/ui/admin/accounts"


def test_the_login_page_sends_someone_logged_in_home(ana):
    assert ana.get("/ui/login").headers["location"] == "/ui/"


def test_logging_out_clears_the_cookie_and_the_login(ana, expect):
    response = ana.post("/ui/logout")
    assert response.status_code == 303
    assert "labelflow_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert ana.get("/ui/").status_code == 303  # not logged in any more
    assert "You are logged out." in expect(ana.get("/ui/login"))


def test_a_form_without_the_token_is_refused(ana, workflow):
    response = ana.post("/ui/projects/xray/workflows/xray_main/next", csrf=False)
    assert response.status_code == 403
    assert "Reload the page" in response.text


# --- home -----------------------------------------------------------------------------------------


def test_home_lists_the_workflows_with_work_waiting(ana, dr_smith, admin, workflow, expect):
    page = expect(ana.get("/ui/"))
    assert "Project xray" in page
    assert "Your roles: annotator" in page
    assert "3 open slots" in page
    assert "Get work" in page
    assert "manage" not in page

    page = expect(dr_smith.get("/ui/"))
    assert "0 open slots" in page  # nothing annotated yet, so nothing to review

    page = expect(admin.get("/ui/"))
    assert "manage" in page
    assert "/ui/admin/projects/xray/workflows/xray_main" in page


def test_home_for_someone_without_a_role(new_person, workflow, expect):
    page = expect(new_person("stranger").get("/ui/"))
    assert "You hold no role in any project yet." in page


def test_home_shows_a_held_slot(ana, workflow, expect):
    assert ana.post("/ui/projects/xray/workflows/xray_main/next").status_code == 303
    page = expect(ana.get("/ui/"))
    assert "you hold slot" in page
    assert "Continue" in page


# --- the labeler's workflow page and asking for work -------------------------------------------


def test_the_workflow_page(ana, dr_smith, new_person, app, workflow, expect):
    page = expect(ana.get("/ui/projects/xray/workflows/xray_main"))
    assert "<h1>Workflow xray_main</h1>" in page
    assert "annotator" in page and "yours" in page
    assert "3 open slots you could claim now" in page
    assert dr_smith.get("/ui/projects/xray/workflows/xray_main").status_code == 200
    # a stranger to the project is told there is no such project; someone in the project
    # but without a role in the workflow is told so
    stranger = new_person("stranger")
    assert stranger.get("/ui/projects/xray/workflows/xray_main").status_code == 404
    engine = app.state.runtime.engine_for("xray")
    engine.define_role("trainee", "root")
    engine.grant_role("stranger", "trainee", "root", "test")
    response = stranger.get("/ui/projects/xray/workflows/xray_main")
    assert response.status_code == 403
    assert "You do not hold a role in this workflow." in response.text
    assert ana.get("/ui/projects/xray/workflows/nope").status_code == 404
    assert ana.get("/ui/projects/nope/workflows/xray_main").status_code == 404


def test_asking_for_work_opens_the_labeling_page(ana, workflow, expect):
    response = ana.post("/ui/projects/xray/workflows/xray_main/next")
    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/ui/projects/xray/workflows/xray_main/slots/\d+", location)
    page = expect(ana.get(location))
    assert 'id="canvas"' in page
    assert "item <strong>img_001</strong>" in page
    work = json.loads(re.search(r'id="work-data">(.*?)</script>', page, re.S).group(1))
    assert work["packet"]["item"] == "img_001"
    assert work["packet"]["prior_labels"] == []
    assert work["shown"]["prior_label_ids"] == []
    assert work["shown"]["show_prior"] is True
    ontology = json.loads(re.search(r'id="ontology-data">(.*?)</script>', page, re.S).group(1))
    assert [m["name"] for m in ontology["masks"]] == ["lung", "effusion"]
    page_data = json.loads(re.search(r'id="page-data">(.*?)</script>', page, re.S).group(1))
    assert page_data["image_url"] == location + "/image"
    assert page_data["api_base"] == location.removeprefix("/ui")
    assert "No earlier label of this item." in page
    # asking again shows the same slot
    assert ana.post("/ui/projects/xray/workflows/xray_main/next").headers["location"] == location


def test_no_work_page_says_why(dr_smith, ana, app, workflow, expect):
    page = expect(dr_smith.post("/ui/projects/xray/workflows/xray_main/next"))
    assert "No work for you right now" in page
    assert "Every slot you could take is already claimed" in page
    engine = app.state.runtime.engine_for("xray")
    engine.pause_workflow("xray_main", "root", "scanner recalibration")
    page = expect(ana.post("/ui/projects/xray/workflows/xray_main/next"))
    assert "paused" in page and "scanner recalibration" in page
    engine.resume_workflow("xray_main", "root", "done")
    engine.pause_stage("xray_main", "annotate", "root", "waiting for the expert")
    page = expect(ana.post("/ui/projects/xray/workflows/xray_main/next"))
    assert "Some stages are paused" in page and "waiting for the expert" in page


def test_the_image_is_served_only_to_the_holder_of_the_slot(ana, ben, workflow, media, expect):
    location = ana.post("/ui/projects/xray/workflows/xray_main/next").headers["location"]
    response = ana.get(location + "/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == (media / "xray_512.png").read_bytes()
    assert ben.get(location + "/image").status_code == 403
    assert ben.get(location).status_code == 403
    assert ana.get(location.rsplit("/", 1)[0] + "/999").status_code == 404


def test_images_outside_the_media_folder_and_on_the_web(app, ana, workflow, tmp_path):
    engine = app.state.runtime.engine_for("xray")
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"not for you")
    digest = "0" * 64
    engine.import_items(
        "batch1",
        [
            NewItem(item="escape", uri=f"file://{outside}", content_hash=digest),
            NewItem(item="web", uri="https://images.example.org/x.png", content_hash=digest),
        ],
        "root",
    )
    with engine.store.reading():
        slots = {r["item"]: r["slot"] for r in engine.store.select("slots", workflow="xray_main")}
    engine.claim("xray_main", slots["escape"], "ana")
    location = f"/ui/projects/xray/workflows/xray_main/slots/{slots['escape']}"
    response = ana.get(location + "/image")
    assert response.status_code == 403
    assert "outside the server&#39;s media folder" in response.text
    engine.release("xray_main", slots["escape"], "ana", "cannot be shown")
    engine.claim("xray_main", slots["web"], "ana")
    location = f"/ui/projects/xray/workflows/xray_main/slots/{slots['web']}"
    assert "https://images.example.org/x.png" in ana.get(location).text
    response = ana.get(location + "/image")
    assert response.status_code == 307
    assert response.headers["location"] == "https://images.example.org/x.png"


def test_releasing_needs_a_reason_and_goes_back_to_the_workflow(ana, workflow, expect):
    location = ana.post("/ui/projects/xray/workflows/xray_main/next").headers["location"]
    response = ana.post(location + "/release", {"reason": "  "})
    assert response.status_code == 422
    assert "Say why" in response.text
    response = ana.post(location + "/release", {"reason": "the image is blurred"})
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/projects/xray/workflows/xray_main"
    page = expect(ana.get("/ui/projects/xray/workflows/xray_main"))
    assert "You gave slot" in page
    assert ana.get(location).status_code == 403  # no longer held


def test_submitting_from_the_page_uses_the_api_with_the_cookie(
    ana, workflow, app, expect, simple_label
):
    location = ana.post("/ui/projects/xray/workflows/xray_main/next").headers["location"]
    page = expect(ana.get(location))
    work = json.loads(re.search(r'id="work-data">(.*?)</script>', page, re.S).group(1))
    body = {"label": simple_label(), "shown": work["shown"], "duration_ms": 1500}
    response = ana.post_json(location.removeprefix("/ui") + "/submit", body)
    assert response.status_code == 201, response.text
    label_id = response.json()["label_id"]
    events = app.state.runtime.engine_for("xray").events(type="label_submitted")
    assert [e.label_id for e in events.rows] == [label_id]
    assert events.rows[0].shown == Shown(**work["shown"])
    assert events.rows[0].duration_ms == 1500


# --- review mode --------------------------------------------------------------------------------


def test_the_reviewer_sees_the_earlier_label_as_layers(
    ana, dr_smith, app, workflow, expect, simple_label
):
    engine = app.state.runtime.engine_for("xray")
    slot = engine.request_work("xray_main", "ana")
    packet = engine.work_packet("xray_main", slot.slot, "ana")
    label_id = engine.submit("xray_main", slot.slot, "ana", simple_label(), packet.shown(), 100)
    location = dr_smith.post("/ui/projects/xray/workflows/xray_main/next").headers["location"]
    page = expect(dr_smith.get(location))
    assert "stage <strong>expert_review</strong>" in page
    work = json.loads(re.search(r'id="work-data">(.*?)</script>', page, re.S).group(1))
    assert [p["label"] for p in work["packet"]["prior_labels"]] == [label_id]
    assert work["shown"]["prior_label_ids"] == [label_id]
    assert "the earlier labels offered as layers and the one you started from" in page
    assert 'id="start-from-help"' in page and 'id="clear-all"' in page


def test_the_reviewer_can_start_from_the_earlier_label(
    ana, dr_smith, app, workflow, expect, simple_label
):
    """The page's script sends the start as shown.started_from; the API records it."""
    engine = app.state.runtime.engine_for("xray")
    slot = engine.request_work("xray_main", "ana")
    packet = engine.work_packet("xray_main", slot.slot, "ana")
    earlier = engine.submit("xray_main", slot.slot, "ana", simple_label(), packet.shown(), 100)
    location = dr_smith.post("/ui/projects/xray/workflows/xray_main/next").headers["location"]
    page = expect(dr_smith.get(location))
    work = json.loads(re.search(r'id="work-data">(.*?)</script>', page, re.S).group(1))
    assert "started_from" not in work["shown"]  # nothing is started from until a click
    script = expect(dr_smith.get("/ui/static/label.js"))
    assert "Start from this layer" in script and "shown.started_from = state.startedFrom" in script
    other = {"content_hash": work["shown"]["content_hash"], "show_prior": True}
    refused = dr_smith.post_json(
        location.removeprefix("/ui") + "/submit",
        {"label": simple_label(), "shown": other | {"started_from": earlier}},
    )
    assert refused.status_code == 422
    assert "is not among the earlier labels reported as shown" in refused.json()["detail"]
    body = {"label": simple_label(), "shown": work["shown"] | {"started_from": earlier}}
    response = dr_smith.post_json(location.removeprefix("/ui") + "/submit", body)
    assert response.status_code == 201, response.text
    event = engine.events(type="label_submitted").rows[-1]
    assert event.user == "dr_smith"
    assert event.shown.started_from == earlier
    assert (
        engine.explain("xray_main", event.item)
        .render()
        .count(f"dr_smith started from label {earlier}")
        == 1
    )


def test_a_blind_stage_shows_no_earlier_label(
    ana, dr_smith, app, project, expect, simple_label, blind_config
):
    engine = app.state.runtime.engine_for("xray")
    report = validate_config(blind_config, engine.validation_context())
    engine.start_workflow(report.config, 1, "root")
    slot = engine.request_work("blind", "ana")
    packet = engine.work_packet("blind", slot.slot, "ana")
    engine.submit("blind", slot.slot, "ana", simple_label(), packet.shown(), 100)
    location = dr_smith.post("/ui/projects/xray/workflows/blind/next").headers["location"]
    page = expect(dr_smith.get(location))
    assert "This stage is blind" in page
    assert "start-from-help" not in page and "Start from this layer" not in page
    work = json.loads(re.search(r'id="work-data">(.*?)</script>', page, re.S).group(1))
    assert work["packet"]["prior_labels"] == []
    assert work["shown"] == {
        "content_hash": work["packet"]["content_hash"],
        "prior_label_ids": [],
        "show_prior": False,
        "consensus_rule_version": None,
    }
