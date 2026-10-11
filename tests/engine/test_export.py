"""The two export tables and the sampling report."""

import json

from labelflow.engine import draw

DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}


def test_export_items_one_row_per_item_with_every_status(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=4)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "cat")  # img_001 agreed -> final from annotate
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")  # img_002 disagreed -> adjudicate, waiting
    engine.exclude_item("wf", "img_003", "admin", "blurry")
    rows = {r["item"]: r for r in engine.export_items("wf")}
    assert list(rows) == ["img_001", "img_002", "img_003", "img_004"]
    assert rows["img_001"] == {
        "workflow": "wf",
        "item": "img_001",
        "uri": "file:///data/img_001.png",
        "content_hash": "sha-001",
        "status": "final",
        "final_status": "final",
        "final_label": '{"value":"cat"}',
        "source_stage": "annotate",
        "label_ids": "[1,2]",
        "rule": "annotate is the first listed stage that labeled it (2 labels that agreed; "
        "the agreed label)",
        "config_version": 1,
        "finalized_at": rows["img_001"]["finalized_at"],
    }
    assert rows["img_002"]["status"] == "in_progress" and rows["img_002"]["final_label"] is None
    assert rows["img_003"]["status"] == "excluded"
    work(engine, "wf", "dr_smith", "dog")
    assert engine.export_items("wf")[1]["status"] == "final"


def test_export_labels_one_row_per_label(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=1)
    work(engine, "wf", "ana", "cat", duration_ms=1000)
    work(engine, "wf", "ben", "dog")
    work(engine, "wf", "dr_smith", "dog")
    rows = engine.export_labels("wf")
    assert [(r["label_id"], r["labeler"], r["stage"]) for r in rows] == [
        (1, "ana", "annotate"),
        (2, "ben", "annotate"),
        (3, "dr_smith", "adjudicate"),
    ]
    expert = rows[2]
    assert json.loads(expert["roles"]) == json.loads(expert["roles_at_claim"]) == ["expert"]
    assert json.loads(expert["shown_prior_ids"]) == [1, 2]
    assert expert["show_prior"] is True and expert["shown_consensus_rule"] == "equal-values@1"
    assert (expert["config_version"], expert["ontology_version"]) == (1, "standin-ontology@1")
    assert json.loads(expert["label"]) == {"value": "dog"}
    assert rows[0]["duration_ms"] == 1000 and rows[0]["score_against"] is None
    for row in rows:
        assert all(not isinstance(v, dict | list) for v in row.values())


def test_sampling_report_counts_arrivals_per_stage_and_upstream_labeler(build, cfg, work):
    review = {"name": "review", "input": "annotate", "assign_to": "expert", "sample": 0.5}
    engine = build(
        cfg({"name": "annotate", "assign_to": "annotator"}, review, final=["review", "annotate"]),
        items=10,
    )
    for _ in range(5):
        work(engine, "wf", "ana")
        work(engine, "wf", "ben")
    rows = engine.sampling_report("wf")
    annotate, review_all, *per_labeler = rows
    assert (annotate["arrived"], annotate["admitted"], annotate["rate"]) == (10, 10, 1.0)
    admitted = sum(draw(1234, "review", f"img_{i:03d}") < 0.5 for i in range(1, 11))
    assert (review_all["arrived"], review_all["admitted"]) == (10, admitted)
    assert review_all["upstream_labeler"] is None and review_all["sample"] == 0.5
    assert [r["upstream_labeler"] for r in per_labeler] == ["ana", "ben"]
    assert sum(r["arrived"] for r in per_labeler) == 10
    assert sum(r["admitted"] for r in per_labeler) == admitted
