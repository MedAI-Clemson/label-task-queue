"""The database enforces the engine's rules by itself, checked with raw SQL that bypasses
SqlStore: uniqueness, the one-claim-per-person index, the claim trigger, the append-only log."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from labelflow.engine import events as ev
from labelflow.engine.events import GENESIS_HASH as GENESIS
from labelflow.store import SqlStore

REFUSED = sa.exc.IntegrityError


def slot_row(slot: int, **changes) -> dict:
    row = {
        "slot": slot,
        "workflow": "wf",
        "stage": "annotate",
        "item": f"img_{slot}",
        "index": 1,
        "reserved_for": None,
        "status": "open",
        "claimed_by": None,
        "claimed_at": None,
        "claimed_roles": None,
        "label": None,
        "config_version": 1,
        "admitted_seq": 1,
        "closed_seq": None,
    }
    return row | changes


def label_row(label: int, slot: int, **changes) -> dict:
    row = {
        "label": label,
        "workflow": "wf",
        "stage": "annotate",
        "item": "img_1",
        "slot": slot,
        "user": "ana",
        "roles": ["annotator"],
        "roles_at_claim": ["annotator"],
        "config_version": 1,
        "value": {"value": "cat"},
        "shown_content_hash": "sha-1",
        "shown_prior_ids": [],
        "show_prior": False,
        "shown_consensus_rule": None,
        "label_task": "standin@1",
        "ontology_version": "o@1",
        "duration_ms": None,
        "claimed_at": None,
        "submitted_at": "2026-10-10T09:00:00+00:00",
        "seq": label,
    }
    return row | changes


# --- labels: one per person per item per workflow, one per slot ---------------------------


def test_a_person_cannot_label_an_item_twice_in_a_workflow(engine, sql_insert):
    sql_insert(engine, "labels", label_row(1, slot=1))
    with pytest.raises(
        REFUSED, match=r"UNIQUE constraint failed: labels\.project, labels\.workflow"
    ):
        sql_insert(engine, "labels", label_row(2, slot=2))  # same workflow, item and user


def test_the_same_person_may_label_the_item_in_another_workflow_or_project(engine, sql_insert):
    sql_insert(engine, "labels", label_row(1, slot=1))
    sql_insert(engine, "labels", label_row(2, slot=2, workflow="other"))
    sql_insert(engine, "labels", label_row(1, slot=1), project="elsewhere")
    sql_insert(engine, "labels", label_row(3, slot=3, user="ben"))


def test_a_slot_holds_one_label(engine, sql_insert):
    sql_insert(engine, "labels", label_row(1, slot=1))
    with pytest.raises(REFUSED, match=r"UNIQUE constraint failed: labels\.project, labels\.slot"):
        sql_insert(engine, "labels", label_row(2, slot=1, user="ben"))


def test_label_and_slot_ids_are_unique_within_a_project(engine, sql_insert):
    sql_insert(engine, "labels", label_row(1, slot=1))
    with pytest.raises(REFUSED, match=r"labels\.label"):
        sql_insert(engine, "labels", label_row(1, slot=2, user="ben"))
    sql_insert(engine, "slots", slot_row(1))
    with pytest.raises(REFUSED, match=r"slots\.slot"):
        sql_insert(engine, "slots", slot_row(1))


# --- slots: one claimed slot per person per workflow ---------------------------------------


def claimed(slot: int, user: str, **changes) -> dict:
    return slot_row(slot, status="claimed", claimed_by=user, **changes)


def test_a_person_holds_one_claim_per_workflow(engine, sql_insert):
    sql_insert(engine, "slots", claimed(1, "ana"))
    with pytest.raises(REFUSED, match=r"UNIQUE constraint failed: slots\.project, slots\.workflow"):
        sql_insert(engine, "slots", claimed(2, "ana"))


def test_claims_that_do_not_clash(engine, sql_insert):
    sql_insert(engine, "slots", claimed(1, "ana"))
    sql_insert(engine, "slots", claimed(2, "ben"))  # another person
    sql_insert(engine, "slots", claimed(3, "ana", workflow="other"))  # another workflow
    sql_insert(engine, "slots", claimed(4, "ana"), project="elsewhere")  # another project
    sql_insert(engine, "slots", slot_row(5))  # open slots have nobody
    sql_insert(engine, "slots", slot_row(6, status="filled", claimed_by="ana", label=1))
    sql_insert(engine, "slots", slot_row(7, status="open"))


def test_releasing_frees_the_person_to_claim_again(engine, sql_insert, sql):
    sql_insert(engine, "slots", slot_row(1))
    sql_insert(engine, "slots", slot_row(2))
    sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ana' WHERE slot = 1")
    with pytest.raises(REFUSED, match="UNIQUE"):
        sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ana' WHERE slot = 2")
    sql(engine, "UPDATE slots SET status = 'open', claimed_by = NULL WHERE slot = 1")
    sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ana' WHERE slot = 2")


def test_a_claimed_slot_cannot_be_claimed_again(engine, sql_insert, sql):
    sql_insert(engine, "slots", claimed(1, "ana"))
    with pytest.raises(REFUSED, match="can only be claimed while it is open"):
        sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ben' WHERE slot = 1")
    sql_insert(engine, "slots", slot_row(2, status="filled", claimed_by="ana", label=9))
    with pytest.raises(REFUSED, match="can only be claimed while it is open"):
        sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ben' WHERE slot = 2")
    sql_insert(engine, "slots", slot_row(3, status="cancelled"))
    with pytest.raises(REFUSED, match="can only be claimed while it is open"):
        sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ben' WHERE slot = 3")
    sql_insert(engine, "slots", slot_row(4))
    sql(engine, "UPDATE slots SET status = 'claimed', claimed_by = 'ben' WHERE slot = 4")
    # other changes to a claimed slot are fine
    sql(engine, "UPDATE slots SET claimed_at = 'now' WHERE slot = 4")


# --- events: append-only, in order, one hash per project -----------------------------------


def a_logged_event(store: SqlStore, dataset: str = "d") -> ev.Event:
    with store.transaction():
        return store.append(
            ev.DatasetCreated(at="2026-10-10T09:00:00Z", dataset=dataset, because="test")
        )


def event_row(seq: int, **changes) -> dict:
    row = {
        "seq": seq,
        "type": "dataset_created",
        "at": "2026-10-10T09:00:00+00:00",
        "actor": None,
        "workflow": None,
        "stage": None,
        "item": None,
        "config_version": None,
        "prev_hash": "p",
        "hash": f"h{seq}",
        "event": {},
    }
    return row | changes


def test_events_cannot_be_changed(engine, store, sql):
    a_logged_event(store)
    with pytest.raises(REFUSED, match="append-only: an event cannot be changed"):
        sql(engine, "UPDATE events SET actor = 'mallory'")
    with pytest.raises(REFUSED, match="append-only"):
        sql(engine, "UPDATE events SET event = '{}' WHERE seq = 1")
    with pytest.raises(REFUSED, match="append-only"):
        sql(engine, "UPDATE events SET seq = 5")


def test_events_cannot_be_deleted(engine, store, sql):
    a_logged_event(store)
    with pytest.raises(REFUSED, match="append-only: an event cannot be deleted"):
        sql(engine, "DELETE FROM events")
    with pytest.raises(REFUSED, match="append-only"):
        sql(engine, "DELETE FROM events WHERE seq = 1")
    assert len(store.events()) == 1


def test_an_event_cannot_be_replaced(engine, store, sql):
    """INSERT OR REPLACE would overwrite a row without firing the delete trigger."""
    a_logged_event(store)
    old = store.events()[0]
    with pytest.raises(REFUSED, match="append-only"):
        sql(
            engine,
            "INSERT OR REPLACE INTO events VALUES ('demo', 1, 'dataset_created', 'x', NULL, NULL,"
            " NULL, NULL, NULL, ?, 'forged', '{}')",
            old.prev_hash,
        )
    assert store.events() == [old]


def test_events_must_follow_each_other(engine, store, sql_insert):
    first = a_logged_event(store)
    with pytest.raises(REFUSED, match="must follow the last one"):  # a gap
        sql_insert(engine, "events", event_row(3, prev_hash=first.hash))
    with pytest.raises(REFUSED, match="must follow the last one"):  # a fork
        sql_insert(engine, "events", event_row(1, prev_hash=first.prev_hash))
    with pytest.raises(REFUSED, match="must follow the last one"):  # not linked to the last
        sql_insert(engine, "events", event_row(2, prev_hash="wrong"))
    with pytest.raises(REFUSED, match="must follow the last one"):  # a first event must be 1
        sql_insert(engine, "events", event_row(2), project="new")
    sql_insert(engine, "events", event_row(1, prev_hash=GENESIS), project="new")
    sql_insert(engine, "events", event_row(2, prev_hash=first.hash))


def test_a_hash_is_unique_within_a_project_only(engine, store, sql_insert):
    first = a_logged_event(store)
    with pytest.raises(REFUSED, match=r"events\.project, events\.hash"):
        sql_insert(engine, "events", event_row(2, prev_hash=first.hash, hash=first.hash))
    # Another project may start with an identical event, hence the same hash.
    sql_insert(engine, "events", event_row(1, prev_hash=GENESIS, hash=first.hash), project="b")


def test_the_triggers_are_not_in_the_way_of_appending(store):
    a_logged_event(store, "one")
    a_logged_event(store, "two")
    assert [e.seq for e in store.events()] == [1, 2]
