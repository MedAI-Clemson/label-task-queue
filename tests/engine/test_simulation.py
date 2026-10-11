"""Simulated labelers run the design note's example workflows to completion.

Each simulated labeler keeps asking for work and submitting; its label is the item's true
value with a set probability, chosen with a seeded random generator.
"""

import json
import random
from collections import Counter, defaultdict

import pytest

from labelflow.engine import draw, rebuild, verify_chain
from labelflow.engine.simulate import SimulatedLabeler, simulate

VALUES = ("normal", "effusion", "nodule")


def truth(item: str) -> str:
    return VALUES[int(item.split("_")[1]) % len(VALUES)]


def labels_by_item(engine, workflow):
    grouped = defaultdict(list)
    for row in engine.export_labels(workflow):
        grouped[row["item"]].append(row)
    return grouped


def check_log(engine):
    events = engine.store.events()
    assert verify_chain(events) == len(events)
    assert rebuild(events).snapshot() == engine.store.snapshot()


def test_annotators_label_and_an_expert_reviews_a_20_percent_sample(build, cfg):
    grants = {
        "ana": ("annotator",),
        "ben": ("annotator",),
        "cy": ("annotator", "expert"),  # may review, but never her own items
        "dr_smith": ("expert",),
    }
    config = cfg(
        {"name": "annotate", "assign_to": "annotator"},
        {"name": "expert_review", "input": "annotate", "assign_to": "expert", "sample": 0.2},
        final=["expert_review", "annotate"],
    )
    engine = build(config, items=150, grants=grants, seed=2026)
    labelers = [SimulatedLabeler(u, 0.8) for u in ("ana", "ben", "cy")]
    labelers.append(SimulatedLabeler("dr_smith", 0.95))
    simulate(engine, "wf", labelers, truth, VALUES, random.Random(11))

    items = engine.export_items("wf")
    assert len(items) == 150 and {r["status"] for r in items} == {"final"}
    sampled = {r["item"] for r in items if draw(2026, "expert_review", r["item"]) < 0.2}
    for row in items:
        assert row["source_stage"] == ("expert_review" if row["item"] in sampled else "annotate")
    report = {r["stage"]: r for r in engine.sampling_report("wf") if r["upstream_labeler"] is None}
    assert report["expert_review"]["admitted"] == len(sampled)
    assert 0.12 < report["expert_review"]["rate"] < 0.28

    for item, labels in labels_by_item(engine, "wf").items():
        users = [r["labeler"] for r in labels]
        assert len(users) == len(set(users)), f"{item} was labeled twice by one person"
        assert len(labels) == (2 if item in sampled else 1)
    reviews = [r for r in engine.export_labels("wf") if r["stage"] == "expert_review"]
    assert {r["labeler"] for r in reviews} == {"cy", "dr_smith"}
    for review in reviews:
        assert json.loads(review["shown_prior_ids"])  # the reviewer saw the annotation
    check_log(engine)


def test_two_annotators_and_adjudication_of_disagreements(build, cfg):
    grants = {
        "ana": ("annotator",),
        "ben": ("annotator",),
        "cy": ("annotator",),
        "dr_smith": ("expert",),
        "dr_jones": ("expert",),
    }
    config = cfg(
        {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2},
        {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"},
        final=["adjudicate", "annotate"],
    )
    engine = build(config, items=80, grants=grants, seed=7)
    labelers = [SimulatedLabeler(u, 0.75) for u in ("ana", "ben", "cy")]
    labelers += [SimulatedLabeler(u, 0.9) for u in ("dr_smith", "dr_jones")]
    simulate(engine, "wf", labelers, truth, VALUES, random.Random(5))

    items = {r["item"]: r for r in engine.export_items("wf")}
    assert {r["status"] for r in items.values()} == {"final"}
    labels = labels_by_item(engine, "wf")
    agreed = disagreed = 0
    for item, rows in labels.items():
        annotations = [r for r in rows if r["stage"] == "annotate"]
        assert len(annotations) == 2
        assert len({r["labeler"] for r in rows}) == len(rows)
        values = {json.loads(r["label"])["value"] for r in annotations}
        if len(values) == 1:
            agreed += 1
            assert items[item]["source_stage"] == "annotate"
            assert items[item]["final_label"] == annotations[0]["label"]
            assert json.loads(items[item]["label_ids"]) == [r["label_id"] for r in annotations]
        else:
            disagreed += 1
            (adjudication,) = [r for r in rows if r["stage"] == "adjudicate"]
            assert items[item]["source_stage"] == "adjudicate"
            assert items[item]["final_label"] == adjudication["label"]
            assert json.loads(adjudication["shown_prior_ids"]) == [
                r["label_id"] for r in annotations
            ]
            assert adjudication["shown_consensus_rule"] == "equal-values@1"
    assert agreed > 20 and disagreed > 10
    check_log(engine)


def test_training_round_scores_every_trainee_against_the_gold_key(build, cfg):
    accuracy = {"t1": 1.0, "t2": 0.8, "t3": 0.5, "t4": 0.2}
    grants = {user: ("trainee",) for user in accuracy}
    config = cfg(
        {
            "name": "gold_read",
            "assign_to": "trainee",
            "labels_per_item": "all",
            "score_against": "gold_key",
        },
        final="none",
    )
    engine = build(
        config,
        items=20,
        grants=grants,
        properties=lambda i: {"gold_key": {"value": truth(f"img_{i:03d}")}},
    )
    labelers = [SimulatedLabeler(user, p) for user, p in accuracy.items()]
    submitted = simulate(engine, "wf", labelers, truth, VALUES, random.Random(3))
    assert submitted == 20 * 4

    assert {r["status"] for r in engine.export_items("wf")} == {"none"}
    labels = engine.export_labels("wf")
    assert Counter(r["labeler"] for r in labels) == {user: 20 for user in accuracy}
    expected = Counter(r["labeler"] for r in labels if r["score_agreed"])
    for row in engine.score_report("wf"):
        user = row["labeler"]
        assert (row["labels"], row["scored"], row["no_key"]) == (20, 20, 0)
        assert row["agreed"] == expected[user]
        assert row["fraction_agreed"] == pytest.approx(expected[user] / 20)
        correct = sum(
            json.loads(r["label"])["value"] == truth(r["item"])
            for r in labels
            if r["labeler"] == user
        )
        assert row["agreed"] == correct
    report = {r["labeler"]: r["fraction_agreed"] for r in engine.score_report("wf")}
    assert report["t1"] == 1.0 and report["t4"] < 0.5
    check_log(engine)
