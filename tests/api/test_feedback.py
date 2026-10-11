"""Feedback: explain, the reports, the exports and the audit view."""

from __future__ import annotations

import csv
import io

import pyarrow.parquet as pq
import pytest

from ltq.api import tabular

GOLD_ROUND = {
    "workflow": "gold_round",
    "dataset": "gold",
    "label_task": "standin@1",
    "stages": [
        {
            "name": "gold_read",
            "assign_to": "trainee",
            "labels_per_item": "all",
            "score_against": "gold_key",
        }
    ],
    "final": "none",
}


@pytest.fixture
def gold(admin, ana, project, make_items, label_with, expect):
    """A training round: ana is a trainee and labels four images against a gold key."""
    expect(admin.post(f"{project}/roles", {"role": "trainee"}), 201)
    expect(admin.post(f"{project}/roles/trainee/grant", {"user": "ana", "reason": "training"}))
    expect(admin.post(f"{project}/datasets", {"name": "gold"}), 201)
    items = make_items(4, gold_key={"value": "cat"})
    expect(admin.post(f"{project}/datasets/gold/items", {"items": items}), 201)
    expect(admin.post(f"{project}/workflows", {"config": GOLD_ROUND}), 201)
    flow = f"{project}/workflows/gold_round"
    for answer in ("cat", "cat", "dog", "cat"):
        label_with(ana, flow, answer)
    return flow


@pytest.fixture
def reviewed(admin, ana, dr_smith, workflow, label_with, expect):
    """The review workflow after ana labeled five images and the expert reviewed what came."""
    for _ in range(5):
        label_with(ana, workflow, "cat")
    while label_with(dr_smith, workflow, "dog"):
        pass
    return workflow


# --- explain -----------------------------------------------------------------------------


def test_explaining_an_item_that_waits(admin, workflow, expect):
    result = expect(admin.get(f"{workflow}/items/img_001/explain"))
    explanation = result["explanation"]
    assert explanation["item"] == "img_001"
    assert explanation["status"] == "in_progress"
    assert explanation["final"] is None
    assert [e["type"] for e in explanation["trace"]] == [
        "item_imported",
        "item_entered",
        "admission_decided",
    ]
    assert all(entry["because"] for entry in explanation["trace"])
    (waiting,) = explanation["waiting"]
    assert waiting["stage"] == "annotate"
    assert result["text"].startswith("item img_001  in workflow xray_main  status: in_progress")
    assert "waiting:" in result["text"]


def test_explaining_a_finished_item(admin, reviewed, expect):
    rows = read_csv(admin.get(f"{reviewed}/exports/items"))
    reviewed_item = next(r for r in rows if r["source_stage"] == "expert_review")["item"]
    result = expect(admin.get(f"{reviewed}/items/{reviewed_item}/explain"))
    explanation = result["explanation"]
    assert explanation["status"] == "final"
    assert explanation["final"]["source_stage"] == "expert_review"
    assert explanation["waiting"] == []
    types = [e["type"] for e in explanation["trace"]]
    assert "admission_decided" in types
    assert types[-1] == "item_finalized"
    assert "final label" in result["text"]


def test_explaining_an_unknown_item_or_workflow(admin, workflow, project):
    item = admin.get(f"{workflow}/items/nope/explain")
    assert item.status_code == 404
    assert item.json()["detail"] == "Item 'nope' is not in workflow 'xray_main'."
    assert admin.get(f"{project}/workflows/nope/items/img_001/explain").status_code == 404


# --- reports -----------------------------------------------------------------------------


def test_the_score_report(admin, gold, expect):
    assert expect(admin.get(f"{gold}/reports/scores")) == [
        {
            "workflow": "gold_round",
            "stage": "gold_read",
            "score_against": "gold_key",
            "labeler": "ana",
            "labels": 4,
            "scored": 4,
            "no_key": 0,
            "agreed": 3,
            "fraction_agreed": 0.75,
        }
    ]


def test_the_score_report_of_a_workflow_that_scores_nothing(admin, workflow, expect):
    assert expect(admin.get(f"{workflow}/reports/scores")) == []


def test_the_sampling_report(admin, reviewed, expect):
    rows = expect(admin.get(f"{reviewed}/reports/sampling"))
    overall = {r["stage"]: r for r in rows if r["upstream_labeler"] is None}
    assert overall["annotate"]["arrived"] == 20
    assert overall["annotate"]["admitted"] == 20
    assert overall["annotate"]["rate"] == 1.0
    review = overall["expert_review"]
    assert review["input"] == "annotate"
    assert review["sample"] == 0.2
    assert review["arrived"] == 5  # only the five annotated images have reached the review
    assert review["admitted"] == review["slots"]
    assert review["rate"] == review["admitted"] / 5
    per_labeler = [r for r in rows if r["upstream_labeler"] == "ana"]
    assert [r["stage"] for r in per_labeler] == ["expert_review"]


def test_the_reports_of_an_unknown_workflow(admin, project):
    for report in ("scores", "sampling"):
        assert admin.get(f"{project}/workflows/nope/reports/{report}").status_code == 404


# --- exports -----------------------------------------------------------------------------


def read_csv(response):
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    return list(csv.DictReader(io.StringIO(response.text)))


def read_parquet(response):
    assert response.headers["content-type"] == "application/vnd.apache.parquet"
    return pq.read_table(io.BytesIO(response.content))


def test_the_items_export_as_csv(admin, reviewed, expect):
    response = admin.get(f"{reviewed}/exports/items")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="xray_main_items.csv"'
    rows = read_csv(response)
    assert list(rows[0]) == list(tabular.ITEM_COLUMNS)
    assert len(rows) == 20
    finished = [r["item"] for r in rows if r["status"] == "final"]
    assert finished == [f"img_{n:03d}" for n in range(1, 6)]  # the five that were labeled
    assert {r["status"] for r in rows[5:]} == {"in_progress"}
    first = rows[0]
    assert first["workflow"] == "xray_main"
    assert first["uri"] == "https://images.example.org/img_001.png"
    assert len(first["content_hash"]) == 64


def test_the_labels_export_as_csv(admin, reviewed, expect):
    response = admin.get(f"{reviewed}/exports/labels")
    assert response.headers["content-disposition"] == 'attachment; filename="xray_main_labels.csv"'
    rows = read_csv(response)
    assert list(rows[0]) == list(tabular.LABEL_COLUMNS)
    annotate = [r for r in rows if r["stage"] == "annotate"]
    review = [r for r in rows if r["stage"] == "expert_review"]
    assert len(annotate) == 5
    assert {r["labeler"] for r in annotate} == {"ana"}
    assert {r["labeler"] for r in review} == {"dr_smith"}
    assert annotate[0]["roles"] == '["annotator"]'
    assert review[0]["roles"] == '["expert"]'
    assert annotate[0]["label"] == '{"value":"cat"}'
    assert annotate[0]["label_task"] == "standin@1"
    assert annotate[0]["show_prior"] == "true"
    assert annotate[0]["score_agreed"] == ""  # empty: nothing was scored
    assert annotate[0]["duration_ms"] == "1200"
    assert annotate[0]["submitted_at"] == "2026-10-10T09:00:00+00:00"


def test_the_exports_as_parquet(admin, reviewed, expect):
    items = read_parquet(admin.get(f"{reviewed}/exports/items?format=parquet"))
    assert items.num_rows == 20
    assert items.column_names == list(tabular.ITEM_COLUMNS)
    assert str(items.schema.field("config_version").type) == "int64"
    csv_items = read_csv(admin.get(f"{reviewed}/exports/items"))
    assert items.column("item").to_pylist() == [r["item"] for r in csv_items]
    assert items.column("status").to_pylist() == [r["status"] for r in csv_items]

    labels = read_parquet(admin.get(f"{reviewed}/exports/labels?format=parquet"))
    csv_labels = read_csv(admin.get(f"{reviewed}/exports/labels"))
    assert labels.num_rows == len(csv_labels)
    assert labels.column_names == list(tabular.LABEL_COLUMNS)
    types = {f.name: str(f.type) for f in labels.schema}
    assert types["label_id"] == "int64"
    assert types["show_prior"] == "bool"
    assert types["score_agreed"] == "bool"
    assert labels.column("labeler").to_pylist() == [r["labeler"] for r in csv_labels]
    assert labels.column("duration_ms").to_pylist() == [1200] * labels.num_rows
    assert set(labels.column("score_agreed").to_pylist()) == {None}


def test_scores_are_in_the_labels_export(admin, gold, expect):
    csv_rows = read_csv(admin.get(f"{gold}/exports/labels"))
    assert [r["score_agreed"] for r in csv_rows] == ["true", "true", "false", "true"]
    assert {r["score_against"] for r in csv_rows} == {"gold_key"}
    table = read_parquet(admin.get(f"{gold}/exports/labels?format=parquet"))
    assert table.column("score_agreed").to_pylist() == [True, True, False, True]


def test_the_export_columns_are_the_ones_the_engine_gives(app, reviewed):
    engine = app.state.runtime.engine_for("xray")
    assert list(engine.export_items("xray_main")[0]) == list(tabular.ITEM_COLUMNS)
    assert list(engine.export_labels("xray_main")[0]) == list(tabular.LABEL_COLUMNS)


def test_an_export_with_no_rows_still_has_its_columns(admin, workflow):
    labels_csv = admin.get(f"{workflow}/exports/labels")
    assert labels_csv.text == ",".join(tabular.LABEL_COLUMNS) + "\n"
    table = read_parquet(admin.get(f"{workflow}/exports/labels?format=parquet"))
    assert table.num_rows == 0
    assert table.column_names == list(tabular.LABEL_COLUMNS)
    assert str(table.schema.field("label_id").type) == "int64"


def test_the_csv_is_the_default_and_the_format_is_checked(admin, workflow):
    assert admin.get(f"{workflow}/exports/items?format=csv").status_code == 200
    response = admin.get(f"{workflow}/exports/items?format=xlsx")
    assert response.status_code == 422
    assert response.json()["detail"].startswith("The request is not valid.")


def test_exporting_an_unknown_workflow(admin, project):
    for table in ("items", "labels"):
        assert admin.get(f"{project}/workflows/nope/exports/{table}").status_code == 404


def test_a_value_with_commas_and_quotes_survives_the_csv(admin, project, make_items, ana, expect):
    expect(admin.post(f"{project}/datasets", {"name": "odd"}), 201)
    item = make_items(1)[0] | {"item": 'a,"b"', "uri": "https://x.org/a,b.png"}
    expect(admin.post(f"{project}/datasets/odd/items", {"items": [item]}), 201)
    config = {
        "workflow": "oddly",
        "dataset": "odd",
        "label_task": "standin@1",
        "stages": [{"name": "annotate", "assign_to": "annotator"}],
    }
    expect(admin.post(f"{project}/workflows", {"config": config}), 201)
    rows = read_csv(admin.get(f"{project}/workflows/oddly/exports/items"))
    assert (rows[0]["item"], rows[0]["uri"]) == ('a,"b"', "https://x.org/a,b.png")


# --- the audit view ----------------------------------------------------------------------


def test_the_audit_view_pages_through_the_log(admin, workflow, project, expect):
    first = expect(admin.get(f"{project}/events?limit=10"))
    assert first["limit"] == 10
    assert first["offset"] == 0
    assert first["total"] > 40  # roles, a dataset, 20 imports, the workflow, 20 admissions...
    assert [e["seq"] for e in first["events"]] == list(range(1, 11))
    second = expect(admin.get(f"{project}/events?limit=10&offset=10"))
    assert [e["seq"] for e in second["events"]] == list(range(11, 21))
    assert first["events"][0]["type"] == "role_defined"
    assert all(e["because"] for e in first["events"])


def test_the_audit_view_can_be_filtered(admin, reviewed, project, expect):
    workflow_events = expect(admin.get(f"{project}/events?workflow=xray_main&limit=500"))
    assert {e["workflow"] for e in workflow_events["events"]} == {"xray_main"}
    one_item = expect(admin.get(f"{project}/events?item=img_001&limit=500"))
    assert {e["item"] for e in one_item["events"]} == {"img_001"}
    labels = expect(admin.get(f"{project}/events?type=label_submitted&workflow=xray_main"))
    assert labels["total"] == len(labels["events"]) >= 5
    nothing = expect(admin.get(f"{project}/events?type=no_such_type"))
    assert (nothing["total"], nothing["events"]) == (0, [])


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
def test_a_bad_audit_page_is_refused(admin, project, query):
    assert admin.get(f"{project}/events?{query}").status_code == 422


def test_the_audit_view_of_an_unknown_project(admin):
    assert admin.get("/projects/nope/events").status_code == 404
