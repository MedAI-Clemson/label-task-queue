"""A SQLite store with the same methods as the engine's ``InMemoryStore``.

``SqlStore`` keeps one project's event log and derived tables in a database that may hold
other projects too. It implements the engine's ``Store`` protocol, so
``Engine(SqlStore(engine, "xray"), registry)`` works unchanged.

Transactions: ``transaction()`` opens a connection and begins with ``BEGIN IMMEDIATE`` (see
``connect``), so the write lock is held before the first read; two claimants take turns.
``reading()`` opens a connection with a plain ``BEGIN``: in WAL mode that is a snapshot,
consistent across its statements, that neither waits for the writer nor holds it up. Both
nest (the outermost decides) and are kept per thread; a read outside either uses a
short-lived connection, one snapshot per statement.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import sqlalchemy as sa

from labelflow.engine.errors import BrokenChain
from labelflow.engine.events import EVENT_ADAPTER, GENESIS_HASH, Event, seal, verify_chain
from labelflow.engine.reducer import apply_event
from labelflow.engine.tables import (
    SCHEMA,
    InMemoryTables,
    IntegrityError,
    Row,
    Scalar,
    Table,
    check_value,
)
from labelflow.store.connection import WRITE, is_configured
from labelflow.store.schema import EVENTS, PROJECT, build_metadata

_PROJECT = f"w_{PROJECT}"  # the bound value of the project, named like the other WHERE values
_FILTERS = ("workflow", "item", "type")  # the event columns ``events()`` can filter on

# Statements are built once per table and reused with bound values, because building and
# fingerprinting a SQLAlchemy statement costs more than running it on a small SQLite table.


@cache
def _select_where(table: sa.Table, equal: tuple[str, ...], null: tuple[str, ...]) -> sa.Select:
    """Rows of one project where the ``equal`` columns match and the ``null`` ones are NULL,
    in primary-key order."""
    query = sa.select(table).where(table.c[PROJECT] == sa.bindparam(_PROJECT))
    for column in equal:
        query = query.where(table.c[column] == sa.bindparam(f"w_{column}"))
    for column in null:
        query = query.where(table.c[column].is_(None))
    return query.order_by(*table.primary_key.columns)


@cache
def _update_row(table: sa.Table, key: tuple[str, ...], columns: tuple[str, ...]) -> sa.Update:
    where = [table.c[PROJECT] == sa.bindparam(_PROJECT)]
    where += [table.c[column] == sa.bindparam(f"w_{column}") for column in key]
    values = {c: sa.bindparam(f"v_{c}", type_=table.c[c].type) for c in columns}
    return sa.update(table).where(*where).values(values)


@cache
def _last_event(events: sa.Table) -> sa.Select:
    return (
        sa.select(events.c.seq, events.c.hash)
        .where(events.c[PROJECT] == sa.bindparam(_PROJECT))
        .order_by(events.c.seq.desc())
        .limit(1)
    )


@cache
def _event_query(events: sa.Table, filters: tuple[str, ...], count: bool, paged: bool) -> sa.Select:
    """The project's events (their JSON, in log order) or their count, filtered on
    ``filters``; ``paged`` adds bound ``limit`` and ``offset``."""
    what = sa.func.count() if count else events.c.event
    query = sa.select(what).where(events.c[PROJECT] == sa.bindparam(_PROJECT))
    for column in filters:
        query = query.where(events.c[column] == sa.bindparam(f"w_{column}"))
    if count:
        return query
    query = query.order_by(events.c.seq)
    if paged:
        query = query.limit(sa.bindparam("limit")).offset(sa.bindparam("offset"))
    return query


@cache
def _whole_log(events: sa.Table) -> sa.Select:
    return (
        sa.select(events).where(events.c[PROJECT] == sa.bindparam(_PROJECT)).order_by(events.c.seq)
    )


def _envelope(event: Event) -> dict[str, Any]:
    """The event fields kept in their own columns of the events table."""
    return {
        "seq": event.seq,
        "type": event.type,
        "at": event.at.isoformat(),
        "actor": event.actor,
        "workflow": event.workflow,
        "stage": event.stage,
        "item": event.item,
        "config_version": event.config_version,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
    }


@dataclass(frozen=True)
class Report:
    """What a rebuild or a check of the derived tables found."""

    events: int
    """Events replayed (the chain of all of them verified)."""
    rows: int
    """Rows in the derived tables after replaying the log."""
    differences: list[str] = field(default_factory=list)
    """How the tables differed from a fresh replay of the log; empty when identical."""

    @property
    def identical(self) -> bool:
        return not self.differences


class SqlStore:
    """One project's event log and derived tables in a SQLite database."""

    def __init__(self, engine: sa.Engine, project: str, schema: tuple[Table, ...] = SCHEMA):
        """``engine`` must come from ``labelflow.store.connect``; ``project`` names the project
        whose rows this store reads and writes."""
        if not is_configured(engine):
            raise ValueError(
                "Create the engine with labelflow.store.connect(url), not create_engine."
            )
        if not project:
            raise ValueError("A store needs a project name.")
        self.engine = engine
        self.project = project
        self._schema = {table.name: table for table in schema}
        self._tables = build_metadata(schema).tables
        self._events = self._tables[EVENTS]
        self._local = threading.local()  # .connection: this thread's open transaction or read

    # --- transactions -----------------------------------------------------------------

    @contextmanager
    def _open(self, write: bool) -> Iterator[None]:
        """This thread's transaction (``BEGIN IMMEDIATE``) or read snapshot (``BEGIN``);
        nested inside an open one, that one decides."""
        if getattr(self._local, "connection", None) is not None:
            if write and not self._local.write:
                raise RuntimeError("A transaction cannot start inside store.reading().")
            yield
            return
        options = {WRITE: True} if write else {}
        with self.engine.connect().execution_options(**options) as connection:
            connection.begin()  # for a write: waits here for the write lock
            self._local.connection, self._local.write = connection, write
            try:
                yield
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit() if write else connection.rollback()
            finally:
                self._local.connection = None

    def transaction(self) -> AbstractContextManager[None]:
        return self._open(write=True)

    def reading(self) -> AbstractContextManager[None]:
        return self._open(write=False)

    @contextmanager
    def _connection(self) -> Iterator[sa.Connection]:
        """This thread's open connection, else a short-lived one for a single statement."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            yield connection
        else:
            with self.engine.connect() as connection:
                yield connection

    def _writer(self, what: str) -> sa.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            raise RuntimeError(f"{what}() must be called inside store.transaction().")
        if not self._local.write:
            raise RuntimeError(f"{what}() cannot be called inside store.reading().")
        return connection

    # --- the event log ----------------------------------------------------------------

    def append(self, event: Event) -> Event:
        connection = self._writer("append")
        last = connection.execute(_last_event(self._events), {_PROJECT: self.project}).first()
        sealed = seal(
            event,
            seq=1 if last is None else last.seq + 1,
            prev_hash=GENESIS_HASH if last is None else last.hash,
        )
        apply_event(self, sealed)
        row = {PROJECT: self.project, **_envelope(sealed), "event": sealed.model_dump(mode="json")}
        self._execute(connection, sa.insert(self._events), row, "append")
        return sealed

    def events(
        self,
        *,
        workflow: str | None = None,
        item: str | None = None,
        type: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        params, filters = self._event_params(workflow, item, type)
        if limit is not None:
            params |= {"limit": limit, "offset": offset}
        query = _event_query(self._events, filters, count=False, paged=limit is not None)
        with self._connection() as connection:
            found = [
                EVENT_ADAPTER.validate_python(d)
                for d in connection.execute(query, params).scalars()
            ]
        return found if limit is not None else found[offset:]

    def count_events(
        self, *, workflow: str | None = None, item: str | None = None, type: str | None = None
    ) -> int:
        params, filters = self._event_params(workflow, item, type)
        query = _event_query(self._events, filters, count=True, paged=False)
        with self._connection() as connection:
            return connection.execute(query, params).scalar_one()

    def _event_params(self, *values: str | None) -> tuple[dict[str, Any], tuple[str, ...]]:
        given = {c: v for c, v in zip(_FILTERS, values, strict=True) if v is not None}
        params = {_PROJECT: self.project} | {f"w_{c}": v for c, v in given.items()}
        return params, tuple(given)

    def _verified_log(self, connection: sa.Connection) -> list[Event]:
        """The whole log, after checking that the columns agree with the events' JSON and
        that the fingerprint chain holds (BrokenChain otherwise)."""
        rows = connection.execute(_whole_log(self._events), {_PROJECT: self.project})
        events = []
        for row in rows.mappings():
            event = EVENT_ADAPTER.validate_python(row["event"])
            for column, value in _envelope(event).items():
                if row[column] != value:
                    raise BrokenChain(
                        f"Event {row['seq']} was changed: its column {column} is "
                        f"{row[column]!r}, but the event itself says {value!r}."
                    )
            events.append(event)
        verify_chain(events)
        return events

    # --- the table interface ----------------------------------------------------------

    def get(self, table: str, /, **key: Scalar) -> Row | None:
        spec, sql_table = self._table(table)
        self._check_key(spec, key)
        rows = self._select(sql_table, spec, key)
        return rows[0] if rows else None

    def insert(self, table: str, row: Mapping[str, Any], /) -> None:
        connection = self._writer("insert")
        spec, sql_table = self._table(table)
        if set(row) != set(spec.columns):
            missing = sorted(set(spec.columns) - set(row))
            extra = sorted(set(row) - set(spec.columns))
            raise IntegrityError(f"Row for {table}: missing {missing}, unknown {extra}.")
        stored = {column: check_value(spec, column, row[column]) for column in spec.columns}
        self._execute(connection, sa.insert(sql_table), {PROJECT: self.project, **stored}, table)

    def update(self, table: str, key: Mapping[str, Scalar], changes: Mapping[str, Any], /) -> None:
        connection = self._writer("update")
        spec, sql_table = self._table(table)
        self._check_key(spec, key)
        for column in changes:
            if column not in spec.columns or column in spec.key:
                raise IntegrityError(f"{table}.{column} cannot be updated.")
        if changes:
            params = {_PROJECT: self.project}
            params |= {f"w_{column}": key[column] for column in spec.key}
            params |= {f"v_{c}": check_value(spec, c, value) for c, value in changes.items()}
            statement = _update_row(sql_table, spec.key, tuple(changes))
            found = self._execute(connection, statement, params, table).rowcount
        else:
            found = len(self._select(sql_table, spec, key))
        if not found:
            raise IntegrityError(f"{table} has no row with key {tuple(key[c] for c in spec.key)}.")

    def select(self, table: str, /, **where: Scalar) -> list[Row]:
        spec, sql_table = self._table(table)
        for column in where:
            if column not in spec.columns or spec.column_type(column)[0] == "json":
                raise IntegrityError(f"Cannot filter {table} on {column}.")
        return self._select(sql_table, spec, where)

    def _select(self, sql_table: sa.Table, spec: Table, where: Mapping[str, Scalar]) -> list[Row]:
        """The project's rows whose columns equal ``where`` (None means NULL), by primary key."""
        equal = tuple(c for c, value in where.items() if value is not None)
        null = tuple(c for c, value in where.items() if value is None)
        params = {_PROJECT: self.project} | {f"w_{c}": where[c] for c in equal}
        with self._connection() as connection:
            result = connection.execute(_select_where(sql_table, equal, null), params)
            return [{column: row[column] for column in spec.columns} for row in result.mappings()]

    def snapshot(self) -> dict[str, list[Row]]:
        with self.reading():
            return {
                name: self._select(self._tables[name], spec, {})
                for name, spec in self._schema.items()
            }

    # --- rebuilding the derived tables from the log ------------------------------------

    def rebuild_derived(self) -> Report:
        """Delete this project's derived rows and replay its events through the reducer.

        All in one transaction, after the log has been verified (BrokenChain otherwise, and
        nothing changes). ``differences`` lists what the rebuild corrected.
        """
        with self.transaction():
            connection = self._writer("rebuild_derived")
            events = self._verified_log(connection)
            before = self.snapshot()
            for name in self._schema:
                table = self._tables[name]
                connection.execute(sa.delete(table).where(table.c[PROJECT] == self.project))
            for event in events:
                apply_event(self, event)
            after = self.snapshot()
        return Report(len(events), _count(after), self._compare(before, after))

    def verify_derived(self) -> Report:
        """Compare the derived tables with a fresh replay of the log, changing nothing.

        Raises BrokenChain if the log does not verify.
        """
        with self.reading(), self._connection() as connection:
            events = self._verified_log(connection)
            current = self.snapshot()
        replay = InMemoryTables(tuple(self._schema.values()))
        for event in events:
            apply_event(replay, event)
        fresh = replay.snapshot()
        return Report(len(events), _count(fresh), self._compare(current, fresh))

    def _compare(self, tables: dict[str, list[Row]], log: dict[str, list[Row]]) -> list[str]:
        """Describe how the tables differ from what the log produces (five lines per table
        at most)."""
        lines: list[str] = []
        for name, spec in self._schema.items():
            have = {tuple(row[c] for c in spec.key): row for row in tables[name]}
            want = {tuple(row[c] for c in spec.key): row for row in log[name]}
            found: list[str] = []
            for key in sorted(have.keys() | want.keys()):
                if key not in want:
                    found.append(f"{name} {key}: in the tables, but the log does not produce it")
                elif key not in have:
                    found.append(f"{name} {key}: the log produces it, but the tables lack it")
                elif have[key] != want[key]:
                    columns = [c for c in spec.columns if have[key][c] != want[key][c]]
                    detail = "; ".join(
                        f"{c} is {have[key][c]!r} in the tables, {want[key][c]!r} by the log"
                        for c in columns
                    )
                    found.append(f"{name} {key}: {detail}")
            lines.extend(found[:5])
            if len(found) > 5:
                lines.append(f"{name}: and {len(found) - 5} more differences")
        return lines

    # --- internals --------------------------------------------------------------------

    def _table(self, table: str) -> tuple[Table, sa.Table]:
        try:
            return self._schema[table], self._tables[table]
        except KeyError:
            raise IntegrityError(f"There is no table named {table}.") from None

    @staticmethod
    def _check_key(spec: Table, key: Mapping[str, Scalar]) -> None:
        if set(key) != set(spec.key):
            raise IntegrityError(f"The key of {spec.name} is {spec.key}, not {tuple(key)}.")

    @staticmethod
    def _execute(
        connection: sa.Connection, statement: sa.Executable, params: Mapping[str, Any], table: str
    ) -> sa.CursorResult:
        """Run a write; a refusal by the database (a constraint or a trigger) or a value that
        cannot be stored is reported as the engine's IntegrityError."""
        try:
            return connection.execute(statement, params)
        except sa.exc.IntegrityError as error:
            raise IntegrityError(f"{table}: {error.orig}") from error
        except sa.exc.StatementError as error:
            if isinstance(error.orig, ValueError):  # a value that cannot be stored (NaN in JSON)
                raise IntegrityError(f"{table}: {error.orig}") from error
            raise


def projects(engine: sa.Engine) -> list[str]:
    """The names of the projects that have events in the database."""
    events = build_metadata().tables[EVENTS]
    with engine.connect() as connection:
        query = sa.select(events.c[PROJECT]).distinct().order_by(events.c[PROJECT])
        return list(connection.execute(query).scalars())


def _count(snapshot: dict[str, list[Row]]) -> int:
    return sum(len(rows) for rows in snapshot.values())
