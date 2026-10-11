"""Events: JSON round trips, the fingerprint chain, and rebuilding from the log."""

import itertools

import pytest

from ltq.engine import (
    BrokenChain,
    InMemoryStore,
    event_from_json,
    event_hash,
    event_to_json,
    rebuild,
    verify_chain,
)
from ltq.engine import events as ev


@pytest.fixture
def engine(build, cfg, work):
    engine = build(
        cfg(
            {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2},
            {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"},
            final=["adjudicate", "annotate"],
        )
    )
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    work(engine, "wf", "dr_smith", "cat")
    return engine


def test_every_event_round_trips_through_json(engine):
    events = engine.store.events()
    assert {e.type for e in events} >= {
        "role_defined",
        "role_granted",
        "dataset_created",
        "item_imported",
        "workflow_started",
        "item_entered",
        "admission_decided",
        "slot_claimed",
        "label_submitted",
        "consensus_checked",
        "stage_completed",
        "item_finalized",
    }
    for event in events:
        again = event_from_json(event_to_json(event))
        assert again == event
        assert event_hash(again) == event.hash


def test_events_are_frozen(engine):
    event = engine.store.events()[0]
    with pytest.raises(Exception, match="frozen"):
        event.because = "something else"


def test_every_event_records_the_common_fields(engine):
    for event in engine.store.events():
        assert event.seq is not None and event.at.tzinfo is not None
        assert event.because
        if event.workflow is not None and event.type != "workflow_started":
            assert event.config_version == 1


def test_the_chain_verifies(engine):
    events = engine.store.events()
    assert verify_chain(events) == len(events)
    assert events[0].prev_hash == ev.GENESIS_HASH
    for before, after in itertools.pairwise(events):
        assert after.prev_hash == before.hash


@pytest.mark.parametrize("position", [0, 5, -1])
def test_changing_an_event_breaks_the_chain(engine, position):
    events = engine.store.events()
    events[position] = events[position].model_copy(update={"because": "nothing to see"})
    with pytest.raises(BrokenChain, match=f"Event {events[position].seq} was changed"):
        verify_chain(events)
    with pytest.raises(BrokenChain):
        rebuild(events)


def test_rewriting_an_event_and_its_hash_breaks_the_next_link(engine):
    events = engine.store.events()
    forged = events[3].model_copy(update={"actor": "mallory", "hash": None})
    events[3] = forged.model_copy(update={"hash": event_hash(forged)})
    with pytest.raises(BrokenChain, match="does not follow"):
        verify_chain(events)


def test_removing_or_reordering_events_breaks_the_chain(engine):
    events = engine.store.events()
    with pytest.raises(BrokenChain):
        verify_chain(events[:4] + events[5:])
    with pytest.raises(BrokenChain):
        verify_chain([events[1], events[0], *events[2:]])


def test_rebuilding_from_the_log_gives_identical_state(engine):
    events = engine.store.events()
    rebuilt = rebuild(events)
    assert rebuilt.snapshot() == engine.store.snapshot()
    assert rebuilt.events() == events


def test_rebuilding_from_json_text_gives_identical_state(engine):
    texts = [event_to_json(e) for e in engine.store.events()]
    rebuilt = rebuild(event_from_json(t) for t in texts)
    assert rebuilt.snapshot() == engine.store.snapshot()


def test_rebuild_needs_an_empty_store(engine):
    with pytest.raises(ValueError, match="empty"):
        rebuild(engine.store.events(), store=engine.store)


def test_append_needs_a_transaction():
    store = InMemoryStore()
    event = ev.DatasetCreated(at="2026-10-10T09:00:00Z", dataset="ds", because="test")
    with pytest.raises(RuntimeError, match="transaction"):
        store.append(event)


def test_a_logged_event_cannot_be_appended_again(engine):
    store = engine.store
    with store.transaction(), pytest.raises(ValueError, match="new event"):
        store.append(store.events()[0])
