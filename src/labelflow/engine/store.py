"""Where a project's event log and derived tables live.

A store holds the append-only event log and the derived tables (see ``tables``). Every
engine command runs inside ``store.transaction()``. ``append`` gives an event its place in
the log (``seq`` and the fingerprint chain) and applies it to the tables through the
reducer, in the same transaction, so the tables always match the log.

Reads that put several answers together run inside ``store.reading()``: they see one
consistent state and never take the write lock, so a long report does not hold up claims.

``InMemoryStore`` is used by tests and simulations; ``labelflow.store.SqlStore`` keeps the
same log in SQLite.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from labelflow.engine.errors import BrokenChain
from labelflow.engine.events import GENESIS_HASH, Event, seal
from labelflow.engine.reducer import apply_event
from labelflow.engine.tables import InMemoryTables, Row, Scalar, Tables


class Store(Tables, Protocol):
    """The event log plus the derived tables."""

    def transaction(self) -> AbstractContextManager[None]:
        """Run a command atomically: all of its events and table changes, or none.

        Transactions may nest; only the outermost one commits or rolls back. The in-memory
        store holds a lock for the duration; SQLite takes the write lock first (``BEGIN
        IMMEDIATE``). A transaction cannot start inside ``reading()``.
        """
        ...

    def reading(self) -> AbstractContextManager[None]:
        """Read from one consistent state, without the write lock.

        Every read inside sees the same state, whatever is committed meanwhile (on SQLite a
        snapshot; in memory the lock). Inside a transaction it reads that transaction's
        state. Writes inside it are refused.
        """
        ...

    def append(self, event: Event) -> Event:
        """Log a new event (one without ``seq``/``hash``) and apply it to the tables.

        Returns the event as logged, with its ``seq``, ``prev_hash`` and ``hash``.
        """
        ...

    def events(
        self,
        *,
        workflow: str | None = None,
        item: str | None = None,
        type: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        """The logged events in order, optionally only those about a workflow, an item or
        of one type, and optionally one page of them."""
        ...

    def count_events(
        self, *, workflow: str | None = None, item: str | None = None, type: str | None = None
    ) -> int:
        """How many events ``events`` would return with the same filters."""
        ...

    def snapshot(self) -> dict[str, list[Row]]:
        """Every derived table's rows in primary-key order, as plain comparable data."""
        ...


class InMemoryStore:
    """A store in Python memory. A failed transaction is rolled back completely."""

    def __init__(self) -> None:
        self._tables = InMemoryTables()
        self._events: list[Event] = []
        self._lock = threading.RLock()
        self._writing = 0  # depth of open transactions (held by the thread with the lock)
        self._reads = 0  # depth of open read contexts outside a transaction

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._writing:
                self._writing += 1
                try:
                    yield
                finally:
                    self._writing -= 1
                return
            if self._reads:
                raise RuntimeError("A transaction cannot start inside store.reading().")
            mark = len(self._events)
            self._writing = 1
            self._tables.begin()
            try:
                yield
            except BaseException:
                self._tables.rollback()
                del self._events[mark:]
                raise
            else:
                self._tables.commit()
            finally:
                self._writing = 0

    def transaction(self) -> AbstractContextManager[None]:
        return self._transaction()

    @contextmanager
    def _reading(self) -> Iterator[None]:
        with self._lock:
            self._reads += 1
            try:
                yield
            finally:
                self._reads -= 1

    def reading(self) -> AbstractContextManager[None]:
        return self._reading()

    def _writable(self, what: str) -> None:
        if not self._writing:
            where = "inside store.reading()" if self._reads else "outside store.transaction()"
            raise RuntimeError(f"{what}() cannot be called {where}.")

    def append(self, event: Event) -> Event:
        self._writable("append")
        last = self._events[-1] if self._events else None
        sealed = seal(
            event,
            seq=1 if last is None else last.seq + 1,
            prev_hash=GENESIS_HASH if last is None else last.hash,
        )
        apply_event(self._tables, sealed)
        self._events.append(sealed)
        return sealed

    def _matching(self, workflow: str | None, item: str | None, type: str | None) -> list[Event]:
        return [
            e
            for e in self._events
            if (workflow is None or e.workflow == workflow)
            and (item is None or e.item == item)
            and (type is None or e.type == type)
        ]

    def events(
        self,
        *,
        workflow: str | None = None,
        item: str | None = None,
        type: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        with self._lock:
            found = self._matching(workflow, item, type)
        return found[offset : None if limit is None else offset + limit]

    def count_events(
        self, *, workflow: str | None = None, item: str | None = None, type: str | None = None
    ) -> int:
        with self._lock:
            return len(self._matching(workflow, item, type))

    def snapshot(self) -> dict[str, list[Row]]:
        with self._lock:
            return self._tables.snapshot()

    # The table interface, for the reducer and the engine's reads.

    def get(self, table: str, /, **key: Scalar) -> Row | None:
        with self._lock:
            return self._tables.get(table, **key)

    def insert(self, table: str, row: Mapping[str, Any], /) -> None:
        self._writable("insert")
        self._tables.insert(table, row)

    def update(self, table: str, key: Mapping[str, Scalar], changes: Mapping[str, Any], /) -> None:
        self._writable("update")
        self._tables.update(table, key, changes)

    def select(self, table: str, /, **where: Scalar) -> list[Row]:
        with self._lock:
            return self._tables.select(table, **where)


def rebuild(events: Iterable[Event], store: Store | None = None) -> Store:
    """Replay a log into an empty store and return it.

    Each event is appended again, so its ``seq`` and fingerprint are recomputed; if one
    differs from the logged value the log was tampered with, and BrokenChain is raised.
    """
    store = InMemoryStore() if store is None else store
    if store.count_events():
        raise ValueError("rebuild() needs an empty store.")
    with store.transaction():
        for event in events:
            fresh = event.model_copy(update={"seq": None, "prev_hash": None, "hash": None})
            logged = store.append(fresh)
            if (logged.seq, logged.hash) != (event.seq, event.hash):
                raise BrokenChain(
                    f"Event {event.seq} does not match the log when replayed: it, or an event "
                    "before it, was changed or removed."
                )
    return store
