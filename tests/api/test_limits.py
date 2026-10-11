"""Size limits: the request body, an import batch, an item's properties, and a label."""

from __future__ import annotations

import json

import pytest

from labelflow.api.limits import as_size


def set_up(root, expect, make_items, one_stage, count=2):
    """A project with a dataset of ``count`` items and a one-stage workflow for ana."""
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/roles", {"role": "annotator"}), 201)
    expect(root.post("/projects/xray/roles/annotator/grant", {"user": "ana", "reason": "x"}))
    expect(root.post("/projects/xray/datasets", {"name": "batch1"}), 201)
    expect(root.post("/projects/xray/datasets/batch1/items", {"items": make_items(count)}), 201)
    expect(root.post("/projects/xray/workflows", {"config": one_stage("flow")}), 201)
    return "/projects/xray/workflows/flow"


def test_a_request_announced_too_big_is_refused_before_it_is_read(app_with, make_items, expect):
    root, _ = app_with(max_request_bytes=2048)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    response = root.post("/projects/xray/datasets/d/items", {"items": make_items(100)})
    assert response.status_code == 413
    assert response.json()["problems"] == []
    assert response.json()["detail"].startswith("The request is too large (")
    assert "the server accepts at most 2 KB" in response.json()["detail"]
    assert expect(root.get("/projects/xray/datasets"))[0]["items"] == 0


def test_a_request_that_grows_too_big_while_read_is_refused(app_with, make_items, expect):
    root, _ = app_with(max_request_bytes=2048)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    raw = json.dumps({"items": make_items(100)}).encode()
    pieces = iter([raw[:1000], raw[1000:2000], raw[2000:]])  # sent chunked, no Content-Length
    response = root.post(
        "/projects/xray/datasets/d/items",
        content=pieces,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == (
        "The request is too large: the server accepts at most 2 KB. Send fewer items at a "
        "time, or smaller ones."
    )
    assert expect(root.get("/projects/xray/datasets"))[0]["items"] == 0


def test_a_request_within_the_limit_goes_through(app_with, make_items, expect):
    root, _ = app_with(max_request_bytes=2048)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    expect(root.post("/projects/xray/datasets/d/items", {"items": make_items(3)}), 201)


def test_a_batch_with_too_many_items_is_refused(app_with, make_items, expect):
    root, _ = app_with(max_import_items=2)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    response = root.post("/projects/xray/datasets/d/items", {"items": make_items(3)})
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "The batch has 3 items, where the server accepts at most 2 at a time. Send it in "
        "smaller batches."
    )
    expect(root.post("/projects/xray/datasets/d/items", {"items": make_items(2)}), 201)


def test_an_item_with_properties_too_large_is_a_problem_of_the_batch(app_with, make_items, expect):
    root, _ = app_with(max_properties_bytes=64)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    small, big = make_items(2)
    big["properties"] = {"gold_key": "x" * 100}
    response = root.post("/projects/xray/datasets/d/items", {"items": [small, big]})
    assert response.status_code == 422
    assert response.json()["detail"] == "No items were imported: 1 of 2 have problems."
    (problem,) = response.json()["problems"]
    assert problem.startswith("Item 'img_002': its properties are too large (")
    assert "where the limit is 0.1 KB" in problem
    assert expect(root.get("/projects/xray/datasets"))[0]["items"] == 0


def test_a_label_too_large_is_refused_and_the_slot_stays_held(
    app_with, make_items, one_stage, expect
):
    root, ana = app_with(max_label_bytes=64)
    flow = set_up(root, expect, make_items, one_stage)
    work = expect(ana.post(f"{flow}/next"))
    slot = work["slot"]["slot"]
    body = {"label": {"value": "x" * 100}, "shown": work["shown"]}
    response = ana.post(f"{flow}/slots/{slot}/submit", body)
    assert response.status_code == 413
    assert response.json()["detail"].startswith("The label is too large: ")
    assert response.json()["detail"].endswith("where the limit is 0.1 KB.")
    assert expect(ana.get(f"{flow}/slots/{slot}"))["slot"]["status"] == "claimed"
    body["label"] = {"value": "cat"}
    expect(ana.post(f"{flow}/slots/{slot}/submit", body), 201)


@pytest.mark.parametrize(
    ("byte_count", "text"),
    [(512, "0.5 KB"), (2048, "2 KB"), (10702, "10.5 KB"), (20 * 1024 * 1024, "20 MB")],
)
def test_sizes_are_said_in_round_numbers(byte_count, text):
    assert as_size(byte_count) == text
