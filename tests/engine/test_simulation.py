"""Simulated labelers run the design note's example workflows to completion.

Each simulated labeler keeps asking for work and submitting; its label is a correct one
with a set probability, chosen with a seeded random generator. The label factory decides
what "correct" and "wrong" look like, so the same simulation runs the stand-in task and the
real ontology task.
"""

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pytest

from labelflow.engine import draw, rebuild, verify_chain
from labelflow.engine.simulate import SimulatedLabeler, simulate, standin_labels
from labelflow.labeltask import load_ontology, rle

VALUES = ("normal", "effusion", "nodule")
ONTOLOGY = Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "chest_xray.yaml"


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
    rng = random.Random(11)
    simulate(engine, "wf", labelers, standin_labels(truth, VALUES, rng), rng)

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
    rng = random.Random(5)
    simulate(engine, "wf", labelers, standin_labels(truth, VALUES, rng), rng)

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
    rng = random.Random(3)
    submitted = simulate(engine, "wf", labelers, standin_labels(truth, VALUES, rng), rng)
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


def xray_label(item: str, correct: bool) -> dict:
    """A label for the chest X-ray ontology: odd images are PA with an effusion, even ones
    AP without; a wrong label gets the view wrong."""
    number = int(item.split("_")[1])
    view = "PA" if number % 2 else "AP"
    if not correct:
        view = "AP" if view == "PA" else "PA"
    lung = np.zeros((16, 16), dtype=bool)
    lung[2:14, 3:13] = True
    effusion = np.zeros((16, 16), dtype=bool)
    if number % 2:
        effusion[10:14, 8:13] = True
    return {
        "classifications": {"view": view, "quality": "good"},
        "masks": {"lung": rle.encode(lung), "effusion": rle.encode(effusion)},
    }


def test_the_simulation_runs_the_ontology_task_too(build, cfg):
    """Any label task works: the factory supplies the labels, the engine never looks inside."""
    ontology = load_ontology(ONTOLOGY)
    config = cfg(
        {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2},
        {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"},
        final=["adjudicate", "annotate"],
    ) | {"label_task": ontology.ref}
    engine = build(config, items=24, task=ontology, seed=3)
    labelers = [SimulatedLabeler("ana", 0.7), SimulatedLabeler("ben", 0.7)]
    labelers.append(SimulatedLabeler("dr_smith", 1.0))
    simulate(engine, "wf", labelers, xray_label, random.Random(9))

    items = engine.export_items("wf")
    assert len(items) == 24 and {r["status"] for r in items} == {"final"}
    for row in items:
        label = json.loads(row["final_label"])
        assert label["classifications"]["view"] == ("PA" if int(row["item"][4:]) % 2 else "AP")
    labels = engine.export_labels("wf")
    assert {r["ontology_version"] for r in labels} == {ontology.ontology_version}
    adjudicated = {r["item"] for r in labels if r["stage"] == "adjudicate"}
    disagreed = {
        item
        for item, rows in labels_by_item(engine, "wf").items()
        if len({json.loads(r["label"])["classifications"]["view"] for r in rows[:2]}) == 2
    }
    assert adjudicated == disagreed and 3 < len(disagreed) < 21
    check_log(engine)
