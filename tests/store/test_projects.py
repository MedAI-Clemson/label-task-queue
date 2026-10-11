"""One database holds several projects; each sees only its own rows and has its own chain."""

from __future__ import annotations

from labelflow.engine import InMemoryStore, verify_chain
from labelflow.store import SqlStore, projects


def test_two_projects_stay_separate(make_sql_engine, script, engine):
    # Same commands, same clock: the logs are identical, hash for hash, yet separate.
    north, south = make_sql_engine("north"), make_sql_engine("south")
    script(north)
    script(south)
    make_sql_engine("tidy")  # a third project that only has the setup

    assert north.store.events() == south.store.events()
    assert north.store.snapshot() == south.store.snapshot()
    for name in ("north", "south"):
        events = SqlStore(engine, name).events()
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert verify_chain(events) == len(events)
    assert projects(engine) == ["north", "south", "tidy"]


def test_work_in_one_project_does_not_touch_another(make_sql_engine, script):
    north, south = make_sql_engine("north"), make_sql_engine("south")
    before = south.store.snapshot(), south.store.events()
    script(north)
    assert (south.store.snapshot(), south.store.events()) == before
    assert len(north.store.events()) > len(south.store.events())


def test_a_project_matches_the_memory_store_whatever_else_is_in_the_database(
    build_engine, make_sql_engine, script
):
    script(make_sql_engine("other"))  # a neighbor with its own roles, items and slots
    mine = make_sql_engine("mine")
    memory = build_engine(InMemoryStore())
    script(mine)
    script(memory)
    assert mine.store.snapshot() == memory.store.snapshot()
    assert mine.store.events() == memory.store.events()


def test_keys_and_ids_may_repeat_across_projects(make_sql_engine):
    a, b = make_sql_engine("a"), make_sql_engine("b")
    assert a.store.select("slots") == b.store.select("slots") != []
    assert a.store.get("items", dataset="ds", item="img_1") is not None
    assert b.store.get("items", dataset="ds", item="img_1") is not None


def test_an_empty_project_is_empty(engine, make_sql_engine):
    make_sql_engine("busy")
    empty = SqlStore(engine, "empty")
    assert empty.events() == []
    assert all(rows == [] for rows in empty.snapshot().values())
    assert empty.get("workflows", workflow="wf") is None
