"""A whole labeling project over HTTP, from the first admin to the exports."""

from __future__ import annotations

import csv
import io
import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from labelflow.api import create_app
from labelflow.engine import draw

ROOT = Path(__file__).resolve().parents[2]


def rows_of(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def test_annotators_label_and_an_expert_reviews_a_sample(
    admin, ana, ben, dr_smith, workflow, project, app, label_with, expect
):
    answers = {"ana": "cat", "ben": "cat", "dr_smith": "dog"}
    people = [ana, ben, dr_smith]
    labeled: Counter[str] = Counter()
    idle = 0
    while idle < len(people):  # everyone asks in turn until nobody is offered anything
        idle = 0
        for person in people:
            work = label_with(person, workflow, answers[person.name])
            if work is None:
                idle += 1
            else:
                labeled[person.name] += 1

    items = [f"img_{n:03d}" for n in range(1, 21)]
    sampled = [item for item in items if draw(11, "expert_review", item) < 0.2]
    assert 0 < len(sampled) < 20
    assert labeled["ana"] + labeled["ben"] == 20
    assert abs(labeled["ana"] - labeled["ben"]) <= 2  # the work was shared
    assert labeled["dr_smith"] == len(sampled)

    # progress: everything is finished
    detail = expect(admin.get(workflow))
    assert detail["progress"]["items"] == {
        "entered": 20,
        "in_progress": 0,
        "final": 20,
        "unresolved": 0,
        "none": 0,
        "excluded": 0,
    }
    stages = {s["stage"]: s for s in detail["progress"]["stages"]}
    assert (stages["annotate"]["filled"], stages["annotate"]["open"]) == (20, 0)
    assert (stages["expert_review"]["filled"], stages["expert_review"]["open"]) == (len(sampled), 0)
    for person in people:
        assert person.post(f"{workflow}/next").status_code == 204

    # the items table: the review label wins where it exists
    exported = rows_of(admin.get(f"{workflow}/exports/items"))
    assert [r["item"] for r in exported] == items
    for row in exported:
        review = row["item"] in sampled
        assert row["status"] == "final"
        assert row["source_stage"] == ("expert_review" if review else "annotate")
        assert row["final_label"] == ('{"value":"dog"}' if review else '{"value":"cat"}')
        assert row["config_version"] == "1"
        assert row["finalized_at"]
        label_id = row["label_ids"].strip("[]")
        assert row["rule"] == (
            f"{row['source_stage']} is the first listed stage that labeled it "
            f"(1 label, label {label_id})"
        )

    # the labels table: who labeled, in which role, at which stage
    labels = rows_of(admin.get(f"{workflow}/exports/labels"))
    assert len(labels) == 20 + len(sampled)
    by_stage = Counter((r["stage"], r["labeler"]) for r in labels)
    assert by_stage[("expert_review", "dr_smith")] == len(sampled)
    assert by_stage[("annotate", "ana")] + by_stage[("annotate", "ben")] == 20
    for row in labels:
        assert row["roles"] == (
            '["expert"]' if row["stage"] == "expert_review" else '["annotator"]'
        )
        assert row["shown_content_hash"] and row["config_version"] == "1"
    review_labels = [r for r in labels if r["stage"] == "expert_review"]
    assert all(r["show_prior"] == "true" and r["shown_prior_ids"] != "[]" for r in review_labels)

    # the same tables as Parquet
    parquet_items = pq.read_table(
        io.BytesIO(admin.get(f"{workflow}/exports/items?format=parquet").content)
    )
    parquet_labels = pq.read_table(
        io.BytesIO(admin.get(f"{workflow}/exports/labels?format=parquet").content)
    )
    assert parquet_items.column("item").to_pylist() == items
    assert parquet_items.column("source_stage").to_pylist() == [r["source_stage"] for r in exported]
    assert parquet_labels.column("label_id").to_pylist() == [int(r["label_id"]) for r in labels]
    assert parquet_labels.column("labeler").to_pylist() == [r["labeler"] for r in labels]

    # the sampling report shows the rate that was in fact drawn
    sampling = expect(admin.get(f"{workflow}/reports/sampling"))
    review = next(
        r for r in sampling if r["stage"] == "expert_review" and not r["upstream_labeler"]
    )
    assert (review["arrived"], review["admitted"]) == (20, len(sampled))
    assert review["rate"] == len(sampled) / 20
    assert sum(r["arrived"] for r in sampling if r["upstream_labeler"] in ("ana", "ben")) == 20

    # explain: a reviewed item and an item that was not sampled
    reviewed_item, plain_item = sampled[0], next(i for i in items if i not in sampled)
    explained = expect(admin.get(f"{workflow}/items/{reviewed_item}/explain"))["explanation"]
    assert explained["final"]["source_stage"] == "expert_review"
    admitted = next(
        e
        for e in explained["trace"]
        if e["type"] == "admission_decided" and e["stage"] == "expert_review"
    )
    assert "expert_review.sample = 0.2 (draw " in admitted["because"]
    assert ", admitted)" in admitted["because"]
    plain = expect(admin.get(f"{workflow}/items/{plain_item}/explain"))["explanation"]
    assert plain["final"]["source_stage"] == "annotate"

    # the log: every label, final label and claim is in it, and it rebuilds the tables
    for event_type, count in (
        ("label_submitted", 20 + len(sampled)),
        ("item_finalized", 20),
        ("slot_claimed", 20 + len(sampled)),
    ):
        events = expect(admin.get(f"{project}/events?type={event_type}&limit=1"))
        assert events["total"] == count, event_type
    assert app.state.runtime.engine_for("xray").store.verify_derived().identical


ONTOLOGY_WORKFLOW = """\
workflow: xray_main
dataset: batch1
label_task: image_ontology@1
stages:
  - name: annotate
    assign_to: annotator
"""


def test_a_project_with_an_ontology_folder(settings, tmp_path):
    """The same flow with the real ontology task, loaded from LABELFLOW_LABEL_TASKS."""
    password = "correct horse battery"
    ontologies = tmp_path / "ontologies"
    ontologies.mkdir()
    shutil.copy(ROOT / "examples" / "ontologies" / "chest_xray.yaml", ontologies)
    app = create_app(replace(settings, label_tasks=ontologies))
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.accounts.create("root", password, is_admin=True)
        runtime.accounts.create("ana", password)
        admin = {"Authorization": f"Bearer {runtime.identity.login('root', password).token}"}
        ana = {"Authorization": f"Bearer {runtime.identity.login('ana', password).token}"}

        def call(headers, path, body=None, status=200):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == status, response.text
            return response.json() if response.content else None

        call(admin, "/projects", {"name": "xray"}, 201)
        call(admin, "/projects/xray/roles", {"role": "annotator"}, 201)
        call(admin, "/projects/xray/roles/annotator/grant", {"user": "ana", "reason": "hired"})
        call(admin, "/projects/xray/datasets", {"name": "batch1"}, 201)
        item = {"item": "a", "uri": "https://x.org/a.png", "content_hash": "ab" * 32}
        call(admin, "/projects/xray/datasets/batch1/items", {"items": [item]}, 201)
        valid = call(admin, "/projects/xray/workflows/validate", {"config": ONTOLOGY_WORKFLOW})
        assert valid["ok"], valid["errors"]
        call(admin, "/projects/xray/workflows", {"config": ONTOLOGY_WORKFLOW, "seed": 3}, 201)

        flow = "/projects/xray/workflows/xray_main"
        work = call(ana, f"{flow}/next")
        assert work["packet"]["item"] == "a"
        wrong = {"label": {"classifications": {"view": "side"}}, "shown": work["shown"]}
        refused = client.post(f"{flow}/slots/1/submit", json=wrong, headers=ana)
        assert refused.status_code == 422
        assert len(refused.json()["problems"]) >= 2  # a wrong value, and the masks left out
        empty = {"size": [10, 10], "counts": [100]}
        label = {
            "classifications": {"view": "PA"},
            "masks": {"lung": {"size": [10, 10], "counts": [0, 100]}, "effusion": empty},
        }
        body = {"label": label, "shown": work["shown"], "duration_ms": 9000}
        assert call(ana, f"{flow}/slots/1/submit", body, 201) == {"label_id": 1}
