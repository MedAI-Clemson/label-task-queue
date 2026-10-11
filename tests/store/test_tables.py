"""The table interface: the in-memory tables and the SQLite tables behave the same.

The shared tests run on both stores. The rest check what only one of them can get wrong:
SQLite's column types and ordering, and the in-memory undo journal.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from labelflow.engine import SCHEMA, InMemoryStore, InMemoryTables, IntegrityError, Table
from labelflow.store import SqlStore, create_schema


def slot_row(slot: int, **changes) -> dict:
    row = {
        "slot": slot,
        "workflow": "wf",
        "stage": "annotate",
        "item": "a",
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


def label_row(label: int, slot: int, user: str) -> dict:
    return {
        "label": label,
        "workflow": "wf",
        "stage": "annotate",
        "item": "a",
        "slot": slot,
        "user": user,
        "roles": ["annotator"],
        "roles_at_claim": ["annotator"],
        "config_version": 1,
        "value": {"value": "cat"},
        "shown_content_hash": "h",
        "shown_prior_ids": [],
        "show_prior": True,
        "shown_consensus_rule": None,
        "started_from": None,
        "label_task": "standin@1",
        "ontology_version": "o@1",
        "duration_ms": None,
        "claimed_at": None,
        "submitted_at": "2026-10-10T09:00:00+00:00",
        "seq": label,
    }


@pytest.fixture(params=["memory", "sqlite"])
def tables(request, store):
    """A store of either kind, empty, with the default schema."""
    return InMemoryStore() if request.param == "memory" else store


# --- shared behaviour -----------------------------------------------------------------------


def test_insert_get_update_select(tables):
    with tables.transaction():
        tables.insert("slots", slot_row(2))
        tables.insert("slots", slot_row(1, item="b"))
        assert tables.get("slots", slot=1)["item"] == "b"
        assert tables.get("slots", slot=3) is None
        tables.update("slots", {"slot": 1}, {"status": "claimed", "claimed_by": "ana"})
    assert [r["slot"] for r in tables.select("slots")] == [1, 2]  # primary-key order
    assert [r["slot"] for r in tables.select("slots", status="open")] == [2]
    assert [r["slot"] for r in tables.select("slots", status="claimed", claimed_by="ana")] == [1]
    assert tables.select("slots", status="claimed", claimed_by="ben") == []
    assert [r["slot"] for r in tables.select("slots", claimed_by=None)] == [2]


def test_returned_rows_are_copies(tables):
    detail = {"values": ["a"]}
    consensus = {
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
    }
    with tables.transaction():
        tables.insert("counters", {"name": "slot", "value": 1})
        tables.insert("consensus", consensus)
    detail["values"].append("b")
    tables.get("counters", name="slot")["value"] = 99
    tables.get("consensus", workflow="wf", stage="s", item="i")["detail"]["values"].append("c")
    assert tables.get("counters", name="slot")["value"] == 1
    assert tables.get("consensus", workflow="wf", stage="s", item="i")["detail"] == {
        "values": ["a"]
    }


def test_an_int_in_a_float_column_comes_back_as_a_float(tables):
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
    with tables.transaction():
        tables.insert("admissions", row)
    assert type(tables.get("admissions", workflow="wf", stage="s", item="i")["threshold"]) is float


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (lambda t: t.insert("slots", slot_row(1)), "already has a row|UNIQUE constraint failed"),
        (lambda t: t.insert("slots", {"slot": 5}), "missing"),
        (lambda t: t.insert("slots", slot_row(5) | {"index": "one"}), "int"),
        (lambda t: t.insert("slots", slot_row(5) | {"status": None}), "None"),
        (lambda t: t.insert("slots", slot_row(5) | {"surprise": 1}), "unknown"),
        (lambda t: t.update("slots", {"slot": 9}, {"status": "open"}), "no row"),
        (lambda t: t.update("slots", {"slot": 9}, {}), "no row"),
        (lambda t: t.update("slots", {"slot": 1}, {"slot": 2}), "cannot be updated"),
        (lambda t: t.update("slots", {"slot": 1}, {"nope": 2}), "cannot be updated"),
        (lambda t: t.update("slots", {"item": "a"}, {"status": "open"}), "key"),
        (lambda t: t.get("slots", item="a"), "key"),
        (lambda t: t.select("consensus", detail={}), "filter"),
        (lambda t: t.select("slots", nope=1), "filter"),
        (lambda t: t.select("nope"), "no table"),
        (lambda t: t.get("nope", x=1), "no table"),
    ],
)
def test_misuse_raises_an_integrity_error(tables, action, message):
    with tables.transaction():
        tables.insert("slots", slot_row(1))
        with pytest.raises(IntegrityError, match=message):
            action(tables)


def test_unique_constraints_back_up_the_rules(tables):
    with tables.transaction():
        tables.insert("labels", label_row(1, slot=1, user="ana"))
        with pytest.raises(IntegrityError, match=r"ana|UNIQUE"):
            tables.insert("labels", label_row(2, slot=2, user="ana"))
        with pytest.raises(IntegrityError, match=r"slot|UNIQUE"):
            tables.insert("labels", label_row(2, slot=1, user="ben"))
        tables.insert("labels", label_row(2, slot=2, user="ben"))
        with pytest.raises(IntegrityError, match=r"already has a row|UNIQUE"):
            tables.update("labels", {"label": 2}, {"user": "ana"})
        tables.update("labels", {"label": 2}, {"stage": "review"})
    assert [r["stage"] for r in tables.select("labels")] == ["annotate", "review"]


def test_a_refused_write_does_not_end_the_transaction(tables):
    with tables.transaction():
        tables.insert("slots", slot_row(1))
        with pytest.raises(IntegrityError):
            tables.insert("slots", slot_row(1))
        tables.insert("slots", slot_row(2))
    assert [r["slot"] for r in tables.select("slots")] == [1, 2]


def test_writes_need_a_transaction(tables):
    with pytest.raises(RuntimeError, match=r"insert\(\)"):
        tables.insert("slots", slot_row(1))
    with pytest.raises(RuntimeError, match=r"update\(\)"):
        tables.update("slots", {"slot": 1}, {"status": "open"})
    assert tables.select("slots") == []


def test_every_schema_table_exists_and_starts_empty(tables):
    assert tables.snapshot() == {table.name: [] for table in SCHEMA}


def test_every_table_has_its_key_among_its_columns():
    for table in SCHEMA:
        assert set(table.key) <= set(table.columns)
        for spec in table.columns.values():
            assert spec.rstrip("?") in {"int", "float", "str", "bool", "time", "json"}


# --- the in-memory undo journal -------------------------------------------------------------


def test_rollback_undoes_inserts_and_updates():
    memory = InMemoryTables()
    memory.insert("slots", slot_row(1))
    before = memory.snapshot()
    memory.begin()
    memory.insert("slots", slot_row(2))
    memory.update("slots", {"slot": 1}, {"status": "claimed", "claimed_by": "ana"})
    memory.rollback()
    assert memory.snapshot() == before
    assert memory.select("slots", status="claimed") == []
    assert [r["slot"] for r in memory.select("slots", status="open")] == [1]


# --- SQLite column types and ordering -------------------------------------------------------

# One column of every type, each with and without None.
THINGS = Table(
    "things",
    key=("id",),
    columns={
        "id": "int",
        "number": "int",
        "number_or_none": "int?",
        "ratio": "float",
        "ratio_or_none": "float?",
        "name": "str",
        "name_or_none": "str?",
        "flag": "bool",
        "flag_or_none": "bool?",
        "at": "time",
        "at_or_none": "time?",
        "data": "json",
    },
    unique=(("name", "number"),),
)


def thing(id: int, **changes) -> dict:
    row = {
        "id": id,
        "number": 7,
        "number_or_none": None,
        "ratio": 0.1,
        "ratio_or_none": None,
        "name": f"thing {id}",
        "name_or_none": None,
        "flag": True,
        "flag_or_none": None,
        "at": "2026-10-10T09:00:00+00:00",
        "at_or_none": None,
        "data": {"a": [1, 2.5, None, "x"], "b": {}},
    }
    return row | changes


@pytest.fixture
def things(engine: sa.Engine) -> SqlStore:
    schema = (THINGS,)
    create_schema(engine, schema)
    return SqlStore(engine, "demo", schema)


def test_every_column_type_comes_back_as_it_went_in(things):
    rows = [
        thing(1),
        thing(
            2,
            number=-(2**62),
            number_or_none=0,
            ratio=1e-9,
            ratio_or_none=-0.0,
            name="Zoë 日本語 \u0000 'quoted' \"double\"",
            name_or_none="",
            flag=False,
            flag_or_none=False,
            at_or_none="2026-10-10T09:00:00+00:00",
            data=None,
        ),
        thing(3, ratio=1.0, name="123", name_or_none="0.5", data=1.0),
        thing(4, flag_or_none=True, data=[True, False, None, 1, 1.0, "1.0"]),
        thing(5, data="a string"),
        thing(6, data=False),
    ]
    memory = InMemoryTables((THINGS,))
    with things.transaction():
        for row in rows:
            things.insert("things", row)
            memory.insert("things", row)
    assert things.snapshot() == memory.snapshot()
    for row in rows:
        assert things.get("things", id=row["id"]) == memory.get("things", id=row["id"])
    stored = things.get("things", id=3)
    assert type(stored["ratio"]) is float and type(stored["data"]) is float
    assert type(stored["flag"]) is bool and type(stored["number"]) is int
    assert things.get("things", id=2)["ratio_or_none"] == 0.0
    assert things.get("things", id=2)["data"] is None


_ids = itertools.count(1)
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**62), 2**62)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(value=json_values)
def test_any_json_value_round_trips(things, value):
    id = next(_ids)
    with things.transaction():
        things.insert("things", thing(id, data=value))
    assert things.get("things", id=id)["data"] == value
    # Equal also in type: 1 stays 1 and 1.0 stays 1.0 (== alone would not tell them apart).
    assert repr(things.get("things", id=id)["data"]) == repr(value)


def test_select_returns_rows_in_primary_key_order_and_filters_on_none(things):
    ids = list(range(1, 41))
    random.Random(5).shuffle(ids)
    memory = InMemoryTables((THINGS,))
    with things.transaction():
        for i in ids:
            row = thing(i, name=f"n{i % 7}", number=i, flag=i % 2 == 0)
            row |= {"number_or_none": None if i % 3 else i, "ratio": i / 4}
            things.insert("things", row)
            memory.insert("things", row)
    assert [r["id"] for r in things.select("things")] == sorted(ids)
    for where in (
        {"flag": True},
        {"flag": False, "name": "n3"},
        {"number_or_none": None},
        {"number_or_none": None, "flag": True},
        {"ratio": 2.5},
        {"name": "missing"},
        {"name_or_none": None},
    ):
        assert things.select("things", **where) == memory.select("things", **where), where


def test_string_keys_sort_like_python(store):
    names = ["b", "B", "a", "é", "Z", "", "10", "9", "日本", "aa", "a b", "a_b"]
    memory = InMemoryTables()
    with store.transaction():
        for name in names:
            row = {"role": name, "defined_by": None, "defined_at": "t", "seq": 1}
            store.insert("roles", row)
            memory.insert("roles", row)
    assert store.select("roles") == memory.select("roles")
    assert [r["role"] for r in store.select("roles")] == sorted(names)


def test_nan_is_refused_rather_than_stored(things):
    with things.transaction(), pytest.raises(IntegrityError, match="JSON"):
        things.insert("things", thing(1, data=math.nan))
