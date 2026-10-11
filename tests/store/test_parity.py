"""SqlStore and InMemoryStore give the same answers to the same engine commands."""

from __future__ import annotations

from labelflow.engine import InMemoryStore, event_from_json, event_to_json, rebuild, verify_chain
from labelflow.store import SqlStore


def test_the_tables_and_the_log_are_identical_after_the_same_commands(build_engine, script, store):
    memory = build_engine(InMemoryStore())
    sql = build_engine(store)
    script(memory)
    script(sql)
    assert sql.store.snapshot() == memory.store.snapshot()
    assert sql.store.events() == memory.store.events()
    assert any(rows for rows in sql.store.snapshot().values())
    for workflow in ("wf",):
        assert sql.export_items(workflow) == memory.export_items(workflow)
        assert sql.export_labels(workflow) == memory.export_labels(workflow)
        assert sql.sampling_report(workflow) == memory.sampling_report(workflow)
    for item in ("img_1", "img_2", "img_3"):
        assert sql.explain("wf", item) == memory.explain("wf", item)


def test_every_table_the_script_touches_is_compared(build_engine, script, store):
    """A guard for the test above: the script must fill most tables, or it proves little."""
    script(build_engine(store))
    empty = [name for name, rows in store.snapshot().items() if not rows]
    assert set(empty) <= {"scores"}  # no key to score against in this project


def test_events_read_back_equal_and_verify(build_engine, script, store):
    script(build_engine(store))
    events = store.events()
    assert verify_chain(events) == len(events) > 20
    assert store.events(workflow="wf") == [e for e in events if e.workflow == "wf"]
    assert store.events(item="img_1") == [e for e in events if e.item == "img_1"]
    assert store.events(workflow="wf", item="img_1") == [
        e for e in events if e.workflow == "wf" and e.item == "img_1"
    ]
    assert store.events(workflow="nope") == []
    # the JSON text of every event hashes to the stored hash
    for event in events:
        assert event_from_json(event_to_json(event)) == event


def test_replaying_the_log_into_either_kind_of_store_gives_the_same_state(
    build_engine, script, store, engine
):
    sql = build_engine(store)
    script(sql)
    events = sql.store.events()
    in_memory = rebuild(events)
    assert in_memory.snapshot() == sql.store.snapshot()
    fresh = rebuild(events, store=SqlStore(engine, "copy"))
    assert fresh.snapshot() == sql.store.snapshot()
    assert fresh.events() == events


def test_the_engine_works_on_a_store_in_the_middle_of_its_life(build_engine, script, store, engine):
    """A second SqlStore (as another process would open) carries on from the same log."""
    first = build_engine(store)
    script(first)
    events = len(store.events())
    second = SqlStore(engine, "demo")
    assert len(second.events()) == events
    assert second.snapshot() == store.snapshot()
