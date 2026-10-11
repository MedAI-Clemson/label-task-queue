"""The SQL schema, generated from the engine's ``SCHEMA``.

Every derived table in ``ltq.engine.tables.SCHEMA`` becomes a SQL table with the same
columns, so a new table or column in ``SCHEMA`` needs no change here. Two things are added:

- a leading ``project`` column, part of every primary key and unique constraint, so one
  database can hold several projects;
- the ``events`` table, the append-only log, with its triggers.

The database also backs up the engine's rules (see ``TRIGGERS`` and ``build_metadata``): the
log refuses changes, one person holds at most one claim per workflow, and a claimed slot
cannot be claimed again. The Alembic migration (``migrations/versions``) is a written-out copy
of this schema; ``tests/store/test_migrations.py`` fails when the two drift apart.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event

from ltq.engine.events import GENESIS_HASH
from ltq.engine.tables import SCHEMA, Table

PROJECT = "project"
"""The column that tells projects apart in every table."""

EVENTS = "events"
"""The name of the event log table."""


class JsonText(sa.types.TypeDecorator):
    """A JSON value kept as JSON text in a TEXT column.

    SQLite gives a column declared JSON numeric affinity, which would turn the text ``1.0``
    into the integer 1; TEXT keeps every value as written. None is stored as JSON ``null``.
    """

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: sa.engine.Dialect) -> str:
        return json.dumps(value, allow_nan=False)

    def process_result_value(self, value: str | None, dialect: sa.engine.Dialect) -> Any:
        return None if value is None else json.loads(value)


_COLUMN_TYPES: dict[str, type[sa.types.TypeEngine]] = {
    "int": sa.Integer,
    "float": sa.Float,
    "str": sa.Text,
    "bool": sa.Boolean,
    "time": sa.Text,  # an ISO 8601 string, as in the in-memory tables
    "json": JsonText,
}

# The event log is append-only. The third trigger also stops an INSERT OR REPLACE, which
# would overwrite an event without firing the delete trigger.
EVENT_TRIGGERS = {
    "events_no_update": f"""\
CREATE TRIGGER events_no_update BEFORE UPDATE ON {EVENTS}
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: an event cannot be changed.');
END""",
    "events_no_delete": f"""\
CREATE TRIGGER events_no_delete BEFORE DELETE ON {EVENTS}
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: an event cannot be deleted.');
END""",
    "events_in_order": f"""\
CREATE TRIGGER events_in_order BEFORE INSERT ON {EVENTS}
WHEN NEW.seq IS NOT COALESCE((SELECT MAX(seq) FROM {EVENTS} WHERE project = NEW.project), 0) + 1
    OR NEW.prev_hash IS NOT COALESCE(
        (SELECT hash FROM {EVENTS} WHERE project = NEW.project AND seq = NEW.seq - 1),
        '{GENESIS_HASH}')
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: a new event must follow the last one.');
END""",
}

# The engine checks that a slot is open before it claims it; this is the database's second
# lock on the same door, should two claimants ever get past the write lock.
SLOT_TRIGGERS = {
    "slots_claim_only_when_open": """\
CREATE TRIGGER slots_claim_only_when_open BEFORE UPDATE OF status ON slots
WHEN NEW.status = 'claimed' AND OLD.status <> 'open'
BEGIN
    SELECT RAISE(ABORT, 'A slot can only be claimed while it is open.');
END""",
}

TRIGGERS = EVENT_TRIGGERS | SLOT_TRIGGERS
"""Every trigger by name; the migration holds the same text."""


def _types(table: Table) -> dict[str, sa.types.TypeEngine]:
    return {column: _COLUMN_TYPES[table.column_type(column)[0]]() for column in table.columns}


def _add_table(metadata: sa.MetaData, table: Table) -> sa.Table:
    if PROJECT in table.columns or table.name == EVENTS:
        raise ValueError(f"The table {table.name!r} clashes with the store's own names.")
    types = _types(table)
    columns = [sa.Column(PROJECT, sa.Text(), nullable=False)]
    for column in table.columns:
        nullable = table.column_type(column)[1]
        columns.append(sa.Column(column, types[column], nullable=nullable))
    constraints: list[sa.schema.SchemaItem] = [
        sa.PrimaryKeyConstraint(PROJECT, *table.key, name=f"pk_{table.name}")
    ]
    for columns_ in table.unique:
        name = f"uq_{table.name}_{'_'.join(columns_)}"
        constraints.append(sa.UniqueConstraint(PROJECT, *columns_, name=name))
    return sa.Table(table.name, metadata, *columns, *constraints)


def _add_events(metadata: sa.MetaData) -> sa.Table:
    """The log: the envelope fields as columns, the whole event as JSON.

    The fingerprint chain is per project, so the hash is unique within a project only (two
    projects may start with identical events).
    """
    return sa.Table(
        EVENTS,
        metadata,
        sa.Column(PROJECT, sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("workflow", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("item", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("event", JsonText(), nullable=False),
        sa.PrimaryKeyConstraint(PROJECT, "seq", name="pk_events"),
        sa.UniqueConstraint(PROJECT, "hash", name="uq_events_project_hash"),
        sa.Index("ix_events_workflow_item", PROJECT, "workflow", "item"),
    )


def _add_slot_rules(slots: sa.Table) -> None:
    """One open claim per person per workflow, and the lookups the engine does all the time."""
    sa.Index(
        "uq_slots_one_claim_per_person",
        slots.c[PROJECT],
        slots.c.workflow,
        slots.c.claimed_by,
        unique=True,
        sqlite_where=sa.text("status = 'claimed'"),
    )
    sa.Index("ix_slots_workflow_status", slots.c[PROJECT], slots.c.workflow, slots.c.status)


def _attach_triggers(table: sa.Table, triggers: dict[str, str]) -> None:
    """Create the triggers right after the table (only when the table is created)."""
    for sql in triggers.values():
        event.listen(table, "after_create", DDL(sql))


_METADATA: dict[tuple, sa.MetaData] = {}


def build_metadata(schema: tuple[Table, ...] = SCHEMA) -> sa.MetaData:
    """The SQLAlchemy tables for a schema, with their constraints, indexes and triggers.

    The result is cached and shared (SQLAlchemy compiles each statement once per table
    object), so treat it as read-only.
    """
    signature = tuple((t.name, t.key, tuple(t.columns.items()), t.unique) for t in schema)
    if signature not in _METADATA:
        metadata = sa.MetaData()
        for table in schema:
            _add_table(metadata, table)
        _attach_triggers(_add_events(metadata), EVENT_TRIGGERS)
        if "slots" in metadata.tables:
            _add_slot_rules(metadata.tables["slots"])
            _attach_triggers(metadata.tables["slots"], SLOT_TRIGGERS)
        _METADATA[signature] = metadata
    return _METADATA[signature]


def create_schema(engine: sa.Engine, schema: tuple[Table, ...] = SCHEMA) -> None:
    """Create every table, index and trigger that does not exist yet (used by tests; real
    databases are created and changed with Alembic)."""
    build_metadata(schema).create_all(engine)
