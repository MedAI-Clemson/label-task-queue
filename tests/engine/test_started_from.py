"""Starting from an earlier label: ``Shown.started_from``, checked, logged and exported."""

import json

import pytest

from labelflow.engine import InvalidShown, Shown, rebuild, verify_chain
from labelflow.engine.events import event_hash, event_to_json

DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}


def adjudication(build, cfg, work, **stage):
    """Two annotations that disagree (labels 1 and 2), and dr_smith's claim to adjudicate."""
    engine = build(cfg(DOUBLE, {**ADJUDICATE, **stage}, final=["adjudicate", "annotate"]), items=1)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    slot = engine.request_work("wf", "dr_smith")
    return engine, slot, engine.work_packet("wf", slot.slot, "dr_smith")


def test_the_label_started_from_is_logged_stored_exported_and_explained(build, cfg, work):
    engine, slot, packet = adjudication(build, cfg, work)
    assert packet.shown().started_from is None
    shown = packet.shown().model_copy(update={"started_from": 2})
    label_id = engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, shown)

    event = [e for e in engine.store.events() if e.type == "label_submitted"][-1]
    assert event.label_id == label_id == 3
    assert event.shown.started_from == 2 and event.shown.prior_label_ids == (1, 2)
    assert engine.store.get("labels", label=label_id)["started_from"] == 2
    assert [r["started_from"] for r in engine.store.select("labels")] == [None, None, 2]

    rows = engine.export_labels("wf")
    assert [r["started_from"] for r in rows] == [None, None, 2]
    assert [r["changed_from_start"] for r in rows[:2]] == [None, None]
    assert json.loads(rows[2]["changed_from_start"]) == {
        "agreed": True,
        "rule_version": "equal-values@1",
        "detail": {"values": ["dog", "dog"]},
    }

    text = engine.explain("wf", "img_001").render()
    assert "label 3 by dr_smith (expert) in adjudicate: " in text
    assert "dr_smith started from label 2" in text
    assert text.count("started from") == 1

    events = engine.store.events()
    assert rebuild(events).snapshot() == engine.store.snapshot()
    assert verify_chain(events) == len(events)


def test_an_edited_start_shows_in_the_export(build, cfg, work):
    engine, slot, packet = adjudication(build, cfg, work)
    shown = packet.shown().model_copy(update={"started_from": 1})
    engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, shown)
    changed = json.loads(engine.export_labels("wf")[2]["changed_from_start"])
    assert changed["agreed"] is False and changed["detail"] == {"values": ["cat", "dog"]}


def test_a_label_without_a_start_keeps_the_old_event_json(build, cfg, work):
    """Events logged before ``started_from`` existed keep their JSON and their hashes."""
    engine, slot, packet = adjudication(build, cfg, work)
    engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, packet.shown())
    event = engine.store.events()[-1]
    submitted = [e for e in engine.store.events() if e.type == "label_submitted"]
    assert all("started_from" not in event_to_json(e) for e in submitted)
    assert event.hash == event_hash(event)
    started = packet.shown().model_copy(update={"started_from": 1})
    assert json.loads(started.model_dump_json())["started_from"] == 1
    assert Shown.model_validate(json.loads(started.model_dump_json())) == started


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"started_from": 7}, r"label 7 is not among the earlier labels .* \(shown: 1 and 2\)"),
        (
            {"started_from": 2, "prior_label_ids": (1,)},
            r"label 2 is not among the earlier labels reported as shown \(shown: 1\)",
        ),
        ({"started_from": 1, "prior_label_ids": ()}, r"\(shown: none\)"),
    ],
)
def test_the_start_must_be_one_of_the_earlier_labels_shown(build, cfg, work, update, message):
    engine, slot, packet = adjudication(build, cfg, work)
    before = len(engine.store.events())
    with pytest.raises(InvalidShown, match=message):
        engine.submit(
            "wf", slot.slot, "dr_smith", {"value": "dog"}, packet.shown().model_copy(update=update)
        )
    assert len(engine.store.events()) == before
    assert engine.slot("wf", slot.slot).claimed_by == "dr_smith"


def test_a_blind_stage_cannot_start_from_an_earlier_label(build, cfg, work):
    engine, slot, packet = adjudication(build, cfg, work, show_prior=False)
    assert not packet.show_prior and packet.prior_labels == ()
    with pytest.raises(InvalidShown, match=r"starting from label 1, .* stage adjudicate is blind"):
        shown = packet.shown().model_copy(update={"started_from": 1})
        engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, shown)
    # Naming the label as shown too is refused by the show_prior check before.
    with pytest.raises(InvalidShown, match="blind"):
        shown = packet.shown().model_copy(
            update={"started_from": 1, "prior_label_ids": (1,), "show_prior": True}
        )
        engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, shown)


def test_a_start_from_the_same_stage_is_refused(build, cfg, work):
    """The first annotation is not shown to the second annotator, so it cannot be a start."""
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=1)
    _, first = work(engine, "wf", "ana", "cat")
    slot = engine.request_work("wf", "ben")
    packet = engine.work_packet("wf", slot.slot, "ben")
    with pytest.raises(InvalidShown, match="labels of annotate itself"):
        shown = packet.shown().model_copy(
            update={"started_from": first, "prior_label_ids": (first,)}
        )
        engine.submit("wf", slot.slot, "ben", {"value": "dog"}, shown)
    with pytest.raises(InvalidShown, match="not among the earlier labels"):
        shown = packet.shown().model_copy(update={"started_from": first})
        engine.submit("wf", slot.slot, "ben", {"value": "dog"}, shown)


def test_a_start_given_as_a_mapping_is_checked_too(build, cfg, work):
    engine, slot, packet = adjudication(build, cfg, work)
    shown = packet.shown().model_dump(mode="json") | {"started_from": "two"}
    with pytest.raises(InvalidShown, match="malformed"):
        engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, shown)
    shown["started_from"] = 1
    assert engine.submit("wf", slot.slot, "dr_smith", {"value": "cat"}, shown) == 3
