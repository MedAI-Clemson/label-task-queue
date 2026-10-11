"""The review example from start to finish in a real browser (milestone 7's exit test).

An administrator sets the project up through the admin pages (accounts, roles, dataset,
items, the workflow with its live check); an annotator logs in, draws with the brush and the
polygon, classifies and submits; the expert reviews the sampled image with the annotator's
label as layers, starts from that layer ("Start from this layer"; undo, redo and "Clear all"
are tried on the way), edits one structure and submits. Then the event log is checked: each
label's ``shown`` is exactly what the page displayed (content hash, earlier label ids,
show_prior, the label started from), the annotator's masks decode to exactly the pixels the
rules say the strokes cover, and the expert's label equals the annotator's except the edited
structure.

It needs Playwright (``pip install playwright==1.48.0``; the version matches the Chromium
build in ``~/.cache/ms-playwright``, else run ``playwright install chromium``) and is skipped
without it. Set ``LABELFLOW_SCREENSHOT_DIR`` to keep screenshots of the pages (docs/ui).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from labelflow.api import create_app
from labelflow.labeltask import rle

playwright = pytest.importorskip("playwright.sync_api")

PASSWORD = "browser-test-pw"
SHOTS = os.environ.get("LABELFLOW_SCREENSHOT_DIR")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(settings, label_tasks):
    """The app under uvicorn in a thread; yields (base url, app)."""
    uvicorn = pytest.importorskip("uvicorn")
    app = create_app(settings, label_tasks=label_tasks)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not instance.started and time.time() < deadline:
        time.sleep(0.05)
    assert instance.started, "the server did not start"
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        instance.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def browser():
    with playwright.sync_playwright() as pw:
        try:
            instance = pw.chromium.launch()
        except Exception as error:  # no browser build: skip, not fail
            pytest.skip(f"Chromium cannot be launched: {error}")
        try:
            yield instance
        finally:
            instance.close()


def shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def login(context, base: str, username: str):
    page = context.new_page()
    page.goto(f"{base}/ui/login")
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/ui/")
    return page


def centers(size: int):
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    return x + 0.5, y + 0.5


def disk(size: int, px: float, py: float, r: float) -> np.ndarray:
    cx, cy = centers(size)
    return (cx - px) ** 2 + (cy - py) ** 2 <= r * r


def rectangle(size: int, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    out = np.zeros((size, size), dtype=bool)
    out[y0:y1, x0:x1] = True
    return out


def test_the_review_example_in_a_browser(server, browser, media, review_config, seed):
    base, app = server
    runtime = app.state.runtime
    runtime.accounts.create("root", PASSWORD, "Root", is_admin=True, created_by=None)
    # --- the administrator sets everything up through the pages ---------------------------
    admin = login(browser.new_context(viewport={"width": 1100, "height": 760}), base, "root")
    admin.goto(f"{base}/ui/admin/accounts")
    for name in ("ana", "dr_smith"):
        admin.fill("#new-username", name)
        admin.fill("#new-display", name.replace("_", " ").title())
        admin.fill("#new-password", PASSWORD)
        admin.click("form.grid-form button[type=submit]")
        admin.wait_for_selector(f"text=Created the account '{name}'.")
    admin.goto(f"{base}/ui/admin/projects")
    admin.fill("#project-name", "xray")
    admin.click("form.row button[type=submit]")
    admin.wait_for_selector("text=Created project 'xray'.")
    for role in ("annotator", "expert"):
        admin.fill("#new-role", role)
        admin.click("#new-role ~ button")
        admin.wait_for_selector(f"text=Defined the role '{role}'.")
    for role, person in (("annotator", "ana"), ("expert", "dr_smith")):
        admin.select_option(f"#grant-user-{role}", person)
        admin.fill(f"#grant-reason-{role}", "pilot")
        admin.click(f"#grant-reason-{role} ~ button")
        admin.wait_for_selector(f"text=Gave the role '{role}' to {person}.")
    admin.fill("#new-dataset", "batch1")
    admin.click("#new-dataset ~ button")
    admin.wait_for_selector("text=Created dataset 'batch1'.")
    admin.fill(
        "#items", "item,uri\nimg_001,xray_512.png\nimg_002,xray_512.png\nimg_003,xray_512.png\n"
    )
    admin.click("form.stack button[type=submit]")
    admin.wait_for_selector("text=Imported 3 items.")
    admin.goto(f"{base}/ui/admin/projects/xray/workflows/new")
    admin.fill("#config", review_config)
    admin.wait_for_selector("#validation-summary:has-text('The config is valid.')")
    assert admin.locator("#validation pre.mermaid").count() == 1
    admin.fill("#seed", str(seed))
    shot(admin, "admin-new-workflow")
    admin.click("#start-form button[type=submit]")
    admin.wait_for_selector(f"text=Started workflow 'xray_main' with seed {seed}.")
    assert "Entered" in admin.content()
    shot(admin, "admin-workflow")

    # --- the annotator labels the first image -----------------------------------------------
    ana = login(browser.new_context(viewport={"width": 1200, "height": 800}), base, "ana")
    assert "3 open slots" in ana.content()
    ana.click("text=Get work")
    ana.wait_for_url("**/slots/*")
    ana.wait_for_function("window.labelflow && window.labelflow.state.image !== null")
    assert ana.locator("#no-prior").count() == 1  # the first stage shows no earlier label

    def api(fn, *args):
        return ana.evaluate("([f, a]) => window.labelflow[f](...a)", [fn, list(args)])

    def client(x, y):
        return api("toClient", x, y)

    api("setView", 1, 40, 40)  # scale 1 with an integer translation, as in the spike harness
    # a brush dot of diameter 11 on 'lung'
    api("selectClass", "lung")
    api("setTool", "brush")
    api("setBrush", 11)
    c = client(100, 100)
    ana.mouse.click(c["x"], c["y"])
    assert api("area", "lung") == 97
    # an 80 x 50 rectangle polygon, closed by clicking its first vertex
    api("setTool", "polygon")
    for x, y in [(320, 320), (400, 320), (400, 370), (320, 370), (320, 320)]:
        c = client(x, y)
        ana.mouse.click(c["x"], c["y"])
    assert api("area", "lung") == 97 + 4000
    # a brush erase out of the rectangle, then undo it through the keyboard
    api("setTool", "brush")
    api("setErase", True)
    c = client(360, 345)
    ana.mouse.click(c["x"], c["y"])
    assert api("area", "lung") < 97 + 4000
    ana.keyboard.press("Control+z")
    assert api("area", "lung") == 97 + 4000
    api("setErase", False)
    ana.select_option("#cls-view", "PA")
    ana.select_option("#cls-quality", "good")
    shot(ana, "labeler")
    ana_url = ana.url
    ana.click("#submit")
    ana.wait_for_function("url => location.href !== url", arg=ana_url)
    ana.wait_for_url("**/slots/*")  # the next slot (img_002) opened by itself

    # --- the expert reviews the sampled image with the annotation as layers ---------------
    expert = login(browser.new_context(viewport={"width": 1200, "height": 800}), base, "dr_smith")
    assert "1 open slot" in expert.content()
    expert.click("text=Get work")
    expert.wait_for_url("**/slots/*")
    expert.wait_for_function("window.labelflow && window.labelflow.state.image !== null")
    assert "stage expert_review" in expert.locator(".toolbar .crumb").inner_text()
    layers = expert.locator("#layers input[data-label]")
    assert layers.count() == 2  # ana's label: one layer per mask class
    shown_ids = sorted(
        {int(v) for v in layers.evaluate_all("els => els.map(e => e.dataset.label)")}
    )
    assert len(shown_ids) == 1
    assert expert.evaluate("window.labelflow.layersShown()") == shown_ids
    expert.uncheck("#layers input[data-class='effusion']")  # a layer can be hidden
    expert.check("#layers input[data-class='effusion']")
    assert "No consensus check" in expert.locator("#flags-caption").inner_text()

    def eapi(fn, *args):
        return expert.evaluate("([f, a]) => window.labelflow[f](...a)", [fn, list(args)])

    def editor():
        """The editor's areas, classification choices and started_from."""
        return (
            eapi("area", "lung"),
            eapi("area", "effusion"),
            expert.locator("#cls-view").input_value(),
            expert.locator("#cls-quality").input_value(),
            eapi("startedFrom"),
        )

    # start from the annotator's label: its masks and classifications fill the editor
    (earlier,) = shown_ids
    start = expert.locator("#layers button[data-start]")
    assert start.count() == 1 and start.get_attribute("data-start") == str(earlier)
    assert "Clear all" in start.get_attribute("title")
    assert expert.locator("#started-from").is_hidden()
    assert editor() == (0, 0, "", "", None)
    start.click()
    started = (97 + 4000, 0, "PA", "good", earlier)
    assert editor() == started
    assert f"You started from label {earlier}" in expert.locator("#started-from").inner_text()
    # one undo step takes the start back, and redo brings it again
    expert.click("#undo")
    assert editor() == (0, 0, "", "", None)
    assert expert.locator("#started-from").is_hidden()
    expert.click("#redo")
    assert editor() == started
    # clearing everything keeps the start recorded (the expert did start from it)
    assert "stays recorded" in expert.locator("#clear-all").get_attribute("title")
    expert.click("#clear-all")
    assert editor() == (0, 0, "", "", earlier)
    assert expert.locator("#started-from").is_visible()
    expert.click("#undo")
    assert editor() == started
    # starting again over a drawing asks first; saying no changes nothing
    asked = []
    expert.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    start.click()
    assert asked and f"label {earlier}" in asked[0]
    assert editor() == started

    # edit one structure: an effusion the annotator did not draw
    eapi("setView", 1, 40, 40)
    eapi("selectClass", "effusion")
    eapi("setTool", "polygon")
    for x, y in [(200, 200), (220, 200), (220, 220), (200, 220)]:
        c = eapi("toClient", x, y)
        expert.mouse.click(c["x"], c["y"])
    expert.keyboard.press("Enter")
    assert eapi("area", "effusion") == 400
    assert eapi("submitBody")["shown"]["started_from"] == earlier
    shot(expert, "reviewer")
    expert_url = expert.url
    expert.click("#submit")
    expert.wait_for_function("url => location.href !== url", arg=expert_url)
    expert.wait_for_selector("text=No work for you right now")

    # --- what the pages reported, checked against the event log ---------------------------
    engine = runtime.engine_for("xray")
    submitted = engine.events(type="label_submitted").rows
    assert [e.user for e in submitted] == ["ana", "dr_smith"]
    content_hash = hashlib.sha256((media / "xray_512.png").read_bytes()).hexdigest()
    first, review = submitted
    assert first.shown.content_hash == content_hash
    assert first.shown.prior_label_ids == ()
    assert first.shown.show_prior is True
    assert first.shown.consensus_rule_version is None
    assert first.duration_ms is not None and first.duration_ms > 0
    assert review.shown.content_hash == content_hash
    assert review.shown.prior_label_ids == (first.label_id,) == tuple(shown_ids)
    assert review.shown.show_prior is True
    assert review.shown.started_from == first.label_id
    assert review.stage == "expert_review" and review.item == "img_001"

    # the annotator's masks are exactly the drawn pixels
    label = first.label
    assert label["classifications"] == {"view": "PA", "quality": "good"}
    lung = rle.decode(label["masks"]["lung"])
    expected = disk(512, 100.5, 100.5, 5.5) | rectangle(512, 320, 320, 400, 370)
    assert int(expected.sum()) == 97 + 4000
    assert np.array_equal(lung, expected)
    assert not rle.decode(label["masks"]["effusion"]).any()
    # the expert's label is the annotator's except the edited structure
    assert review.label["classifications"] == label["classifications"]
    assert np.array_equal(rle.decode(review.label["masks"]["lung"]), lung)
    effusion = rle.decode(review.label["masks"]["effusion"])
    assert np.array_equal(effusion, rectangle(512, 200, 200, 220, 220))
    # and the labels export says so: the start and how far the label moved from it
    exported = engine.export_labels("xray_main")[1]
    assert exported["started_from"] == first.label_id
    changed = json.loads(exported["changed_from_start"])
    assert changed["agreed"] is False
    assert changed["detail"]["masks"]["lung"]["min_iou"] == 1.0
    assert changed["detail"]["masks"]["effusion"]["min_iou"] == 0.0
    assert all(c["agreed"] for c in changed["detail"]["classifications"].values())
    assert (
        f"dr_smith started from label {first.label_id}"
        in engine.explain("xray_main", "img_001").render()
    )

    # the review label is img_001's final label
    explanation = engine.explain("xray_main", "img_001")
    assert explanation.status == "final"
    assert explanation.final.source_stage == "expert_review"
    assert explanation.final.label_ids == (review.label_id,)
    admin.goto(f"{base}/ui/admin/projects/xray/workflows/xray_main/explain?item=img_001")
    assert "final" in admin.locator("h2 .tag").inner_text()
    shot(admin, "admin-explain")
    # the admin pages work at phone width: no horizontal page scroll
    admin.set_viewport_size({"width": 390, "height": 760})
    admin.goto(f"{base}/ui/admin/projects/xray")
    assert admin.evaluate("document.documentElement.scrollWidth") <= 390
    shot(admin, "admin-project-phone")
