"""Transactions: atomic commands, nesting, the write lock taken first, read snapshots, WAL.

What holds on both stores (a failed or refused command leaves no trace, append needs a
transaction, the read context refuses writes) is checked by the engine tests; here is what
only SQLite can get wrong.
"""

from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa

from labelflow.engine import events as ev
from labelflow.store import SqlStore, connect


def a_dataset_event(name: str) -> ev.DatasetCreated:
    return ev.DatasetCreated(at="2026-10-10T09:00:00Z", dataset=name, because="test")


def counter(store: SqlStore, name: str = "slot", value: int = 1) -> None:
    store.insert("counters", {"name": name, "value": value})


def names(store: SqlStore) -> set[str]:
    return {r["name"] for r in store.select("counters")}


# --- write transactions ---------------------------------------------------------------------


def test_an_exception_rolls_back_events_and_rows_together(store):
    with store.transaction():
        counter(store, "kept")
    events, state = store.events(), store.snapshot()
    with pytest.raises(KeyError), store.transaction():
        counter(store, "lost")
        store.append(a_dataset_event("lost"))
        assert len(store.events()) == len(events) + 1  # visible inside the transaction
        raise KeyError("boom")
    assert store.events() == events
    assert store.snapshot() == state


def test_transactions_nest_and_only_the_outermost_decides(store):
    with store.transaction():
        counter(store, "outer")
        with store.transaction():
            counter(store, "inner")
        assert store.get("counters", name="inner") is not None  # not committed yet
    assert names(store) == {"outer", "inner"}

    with pytest.raises(KeyError), store.transaction():
        counter(store, "a")
        with store.transaction():
            counter(store, "b")
        raise KeyError("outer fails after the inner one succeeded")
    assert names(store) == {"outer", "inner"}


def test_a_transaction_after_a_failure_works(store):
    with pytest.raises(KeyError), store.transaction():
        raise KeyError("boom")
    with store.transaction():
        counter(store)
    assert store.get("counters", name="slot") == {"name": "slot", "value": 1}


def test_other_connections_see_a_command_only_after_it_commits(engine, open_engine, store):
    other = SqlStore(open_engine(), "demo")
    with store.transaction():
        counter(store)
        store.append(a_dataset_event("x"))
        assert other.select("counters") == []
        assert other.events() == []
    assert other.get("counters", name="slot") is not None
    assert len(other.events()) == 1


def test_the_write_lock_is_taken_when_the_transaction_begins(engine, open_engine, store):
    """BEGIN IMMEDIATE: a second writer is turned away at once, before it has read or
    written anything. A plain BEGIN would let it in and fail only at its first write."""
    other = SqlStore(open_engine(busy_timeout=0.2), "demo")
    with store.transaction():
        with pytest.raises(sa.exc.OperationalError, match="locked"), other.transaction():
            pytest.fail("the second transaction must not start while the first is open")
        # readers are not blocked by the open write transaction (WAL mode)
        assert other.select("counters") == []
    with other.transaction():  # the lock is free again
        counter(other)


def test_a_second_writer_waits_for_the_first_to_finish(engine, open_engine, store):
    other = SqlStore(open_engine(busy_timeout=10), "demo")
    holding, order = threading.Event(), []

    def second() -> None:
        holding.wait()
        with other.transaction():
            order.append("second")
            counter(other, "second")

    thread = threading.Thread(target=second)
    thread.start()
    with store.transaction():
        holding.set()
        thread.join(timeout=0.5)  # still blocked: waiting for the lock
        assert thread.is_alive()
        order.append("first")
        counter(store, "first")
    thread.join(timeout=10)
    assert order == ["first", "second"]
    assert names(store) == {"first", "second"}


def test_each_thread_has_its_own_transaction(store):
    """Reads in another thread use their own connection, so they see only committed data."""
    seen = []
    with store.transaction():
        counter(store)
        worker = threading.Thread(target=lambda: seen.append(store.select("counters")))
        worker.start()
        worker.join()
        assert len(store.select("counters")) == 1
    assert seen == [[]]


# --- read snapshots -------------------------------------------------------------------------


def test_a_read_sees_one_snapshot_whatever_is_committed_meanwhile(engine, open_engine, store):
    writer = SqlStore(open_engine(), "demo")
    with writer.transaction():
        counter(writer, "before")
    with store.reading():
        assert names(store) == {"before"}
        with writer.transaction():
            counter(writer, "during")
            writer.append(a_dataset_event("during"))
        assert writer.get("counters", name="during") is not None  # committed...
        assert names(store) == {"before"}  # ...but the read keeps its snapshot
        assert store.events() == [] and store.count_events() == 0
    assert names(store) == {"before", "during"}  # a new read sees it
    assert len(store.events()) == 1


def test_a_read_does_not_wait_for_a_writer_nor_hold_one_up(engine, open_engine, store):
    """WAL mode: the writer's uncommitted rows are invisible, and the read holds no lock."""
    writer = SqlStore(open_engine(busy_timeout=0.2), "demo")
    with writer.transaction():
        counter(writer, "pending")
        with store.reading():  # starts at once, although the write lock is held
            assert store.select("counters") == []
            assert store.count_events() == 0
    with store.reading():
        assert names(store) == {"pending"}
        with writer.transaction():  # the writer is not turned away by the open read
            counter(writer, "later")
        assert names(store) == {"pending"}
    assert names(store) == {"pending", "later"}


def test_a_read_takes_no_write_lock(engine, open_engine, store):
    other = SqlStore(open_engine(busy_timeout=0.2), "demo")
    with store.reading(), other.transaction():
        counter(other)  # two readers, or a reader and a writer, never wait for each other
    with store.reading():
        store.select("counters")
        with other.reading():
            other.select("counters")


def test_a_read_nested_in_a_transaction_sees_its_uncommitted_rows(store):
    with store.transaction():
        counter(store, "mine")
        with store.reading():
            assert names(store) == {"mine"}
        counter(store, "after")  # still a write transaction once the nested read is over
    assert names(store) == {"mine", "after"}


def test_reads_leave_no_transaction_behind(store):
    with pytest.raises(KeyError), store.reading():
        store.select("counters")
        raise KeyError("boom")
    with store.transaction():
        counter(store)
    assert names(store) == {"slot"}


# --- the connection settings ----------------------------------------------------------------


def test_the_database_uses_wal_and_waits_for_locks(engine):
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30_000


def test_connections_opened_after_a_dispose_have_the_same_settings(engine, open_engine, store):
    other = SqlStore(open_engine(busy_timeout=0.2), "demo")
    other.engine.dispose()
    store.engine.dispose()
    with store.transaction():
        with pytest.raises(sa.exc.OperationalError, match="locked"), other.transaction():
            pytest.fail("the second transaction must not start while the first is open")
        with other.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 200


def test_connect_settings(url):
    engine = connect(url, busy_timeout=2, synchronous="NORMAL")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 2_000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == 1
    finally:
        engine.dispose()
    with pytest.raises(ValueError, match="SQLite"):
        connect("postgresql://localhost/labelflow")
    with pytest.raises(ValueError, match="synchronous"):
        connect(url, synchronous="sometimes")


def test_a_store_needs_an_engine_from_connect(url):
    plain = sa.create_engine(url)
    try:
        with pytest.raises(ValueError, match=r"connect\(url\)"):
            SqlStore(plain, "demo")
    finally:
        plain.dispose()


def test_a_store_needs_a_project_name(engine):
    with pytest.raises(ValueError, match="project"):
        SqlStore(engine, "")
