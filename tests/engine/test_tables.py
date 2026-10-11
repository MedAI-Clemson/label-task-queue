"""The in-memory table interface that milestone 4 reimplements over SQLite."""

import pytest

from ltq.engine import SCHEMA, InMemoryTables, IntegrityError


def slot_row(slot, status="open", item="a"):
    return {
        "slot": slot,
        "workflow": "wf",
        "stage": "annotate",
        "item": item,
        "index": 1,
        "reserved_for": None,
        "status": status,
        "claimed_by": None,
        "claimed_at": None,
        "label": None,
        "config_version": 1,
        "admitted_seq": 1,
        "closed_seq": None,
    }


def test_insert_get_update_select():
    tables = InMemoryTables()
    tables.insert("slots", slot_row(2))
    tables.insert("slots", slot_row(1, item="b"))
    assert tables.get("slots", slot=1)["item"] == "b"
    assert tables.get("slots", slot=3) is None
    tables.update("slots", {"slot": 1}, {"status": "claimed", "claimed_by": "ana"})
    assert [r["slot"] for r in tables.select("slots")] == [1, 2]  # primary-key order
    assert [r["slot"] for r in tables.select("slots", status="open")] == [2]
    assert [r["slot"] for r in tables.select("slots", status="claimed", claimed_by="ana")] == [1]
    assert tables.select("slots", status="claimed", claimed_by="ben") == []


def test_returned_rows_are_copies():
    tables = InMemoryTables()
    tables.insert(
        "counters",
        {"name": "slot", "value": 1},
    )
    row = tables.get("counters", name="slot")
    row["value"] = 99
    assert tables.get("counters", name="slot")["value"] == 1


def test_json_columns_are_copied_deeply():
    tables = InMemoryTables()
    detail = {"values": ["a"]}
    tables.insert(
        "consensus",
        {
            "workflow": "wf",
            "stage": "s",
            "item": "i",
            "label_ids": [1, 2],
            "agreed": True,
            "agreed_label": {"value": "a"},
            "rule_version": "r",
            "detail": detail,
            "config_version": 1,
            "at": "2026-10-10T09:00:00+00:00",
            "seq": 1,
        },
    )
    detail["values"].append("b")
    tables.get("consensus", workflow="wf", stage="s", item="i")["detail"]["values"].append("c")
    assert tables.get("consensus", workflow="wf", stage="s", item="i")["detail"] == {
        "values": ["a"]
    }


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (lambda t: t.insert("slots", slot_row(1)), "already has a row"),
        (lambda t: t.insert("slots", {"slot": 5}), "missing"),
        (lambda t: t.insert("slots", slot_row(5) | {"index": "one"}), "int"),
        (lambda t: t.insert("slots", slot_row(5) | {"status": None}), "None"),
        (lambda t: t.update("slots", {"slot": 9}, {"status": "open"}), "no row"),
        (lambda t: t.update("slots", {"slot": 1}, {"slot": 2}), "cannot be updated"),
        (lambda t: t.get("slots", item="a"), "key"),
        (lambda t: t.select("consensus", detail={}), "filter"),
        (lambda t: t.select("nope"), "no table"),
    ],
)
def test_schema_violations_raise(action, message):
    tables = InMemoryTables()
    tables.insert("slots", slot_row(1))
    with pytest.raises(IntegrityError, match=message):
        action(tables)


def test_rollback_undoes_inserts_and_updates():
    tables = InMemoryTables()
    tables.insert("slots", slot_row(1))
    before = tables.snapshot()
    tables.begin()
    tables.insert("slots", slot_row(2))
    tables.update("slots", {"slot": 1}, {"status": "claimed", "claimed_by": "ana"})
    tables.rollback()
    assert tables.snapshot() == before
    assert tables.select("slots", status="claimed") == []
    assert [r["slot"] for r in tables.select("slots", status="open")] == [1]


def test_floats_are_stored_as_floats():
    tables = InMemoryTables()
    row = {
        "workflow": "wf",
        "stage": "s",
        "item": "i",
        "source": "dataset",
        "admitted": True,
        "draw": None,
        "threshold": 1,
        "labels_draw": None,
        "n_slots": 1,
        "config_version": 1,
        "at": "2026-10-10T09:00:00+00:00",
        "seq": 1,
        "completed": False,
        "outputs": [],
        "completed_seq": None,
    }
    tables.insert("admissions", row)
    assert isinstance(
        tables.get("admissions", workflow="wf", stage="s", item="i")["threshold"], float
    )


def test_every_table_has_its_key_among_its_columns():
    for table in SCHEMA:
        assert set(table.key) <= set(table.columns)
        for spec in table.columns.values():
            assert spec.rstrip("?") in {"int", "float", "str", "bool", "time", "json"}


def label_row(label, slot, user):
    return {
        "label": label,
        "workflow": "wf",
        "stage": "annotate",
        "item": "a",
        "slot": slot,
        "user": user,
        "roles": ["annotator"],
        "config_version": 1,
        "value": {"value": "cat"},
        "shown_content_hash": "h",
        "shown_prior_ids": [],
        "show_prior": True,
        "shown_consensus_rule": None,
        "label_task": "standin@1",
        "ontology_version": "o@1",
        "duration_ms": None,
        "claimed_at": None,
        "submitted_at": "2026-10-10T09:00:00+00:00",
        "seq": label,
    }


def test_unique_constraints_back_up_the_rules():
    tables = InMemoryTables()
    tables.insert("labels", label_row(1, slot=1, user="ana"))
    with pytest.raises(IntegrityError, match="'user': 'ana'"):
        tables.insert("labels", label_row(2, slot=2, user="ana"))
    with pytest.raises(IntegrityError, match="'slot': 1"):
        tables.insert("labels", label_row(2, slot=1, user="ben"))
    tables.insert("labels", label_row(2, slot=2, user="ben"))
    with pytest.raises(IntegrityError, match="already has a row"):
        tables.update("labels", {"label": 2}, {"user": "ana"})
    tables.update("labels", {"label": 2}, {"stage": "review"})
