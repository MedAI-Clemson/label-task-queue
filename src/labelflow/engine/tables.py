"""Derived state, kept as a small set of relational tables.

The event log is the source of truth. These tables hold the current state derived from it
(slots, labels, final labels, ...), so the engine can answer questions without replaying
the log. They are rebuilt exactly by replaying the log through the reducer.

The engine and the reducer reach the tables only through the ``Tables`` interface below:
get by primary key, insert, update, and select with equality filters. Rows are dicts of
JSON-compatible values; ordering beyond primary-key order is done in Python. The interface
is kept this small so that a SQL implementation (milestone 4) can run the same reducer and
engine unchanged.

Column types: ``int``, ``float``, ``str``, ``bool``, ``time`` (an ISO 8601 UTC string) and
``json`` (any JSON value; never used in filters). A trailing ``?`` means the column may hold
None. ``unique`` lists column sets that must be unique besides the primary key; they back up
the engine's rules the way database constraints will (one label per person per item per
workflow, one label per slot).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

Scalar = str | int | float | bool | None
Row = dict[str, Any]


class IntegrityError(RuntimeError):
    """A table operation broke the schema: a duplicate key, a missing row, a wrong column.

    It means a bug in the engine or the reducer, never a user mistake.
    """


@dataclass(frozen=True)
class Table:
    """A table's name, primary key and columns (name -> type, see the module docstring)."""

    name: str
    key: tuple[str, ...]
    columns: Mapping[str, str]
    unique: tuple[tuple[str, ...], ...] = ()

    def column_type(self, column: str) -> tuple[str, bool]:
        """The column's base type and whether it may hold None."""
        spec = self.columns[column]
        return spec.rstrip("?"), spec.endswith("?")


SCHEMA: tuple[Table, ...] = (
    Table(
        "roles",
        key=("role",),
        columns={"role": "str", "defined_by": "str?", "defined_at": "time", "seq": "int"},
    ),
    Table(
        "role_assignments",
        key=("user", "role"),
        columns={"user": "str", "role": "str", "active": "bool", "since": "time", "seq": "int"},
    ),
    Table(
        "datasets",
        key=("dataset",),
        columns={"dataset": "str", "created_by": "str?", "created_at": "time", "seq": "int"},
    ),
    Table(
        "items",
        key=("dataset", "item"),
        columns={
            "dataset": "str",
            "item": "str",
            "uri": "str",
            "content_hash": "str",
            "properties": "json",
            "imported_by": "str?",
            "imported_at": "time",
            "seq": "int",
        },
    ),
    Table(
        "workflows",
        key=("workflow",),
        columns={
            "workflow": "str",
            "dataset": "str",
            "label_task": "str",
            "seed": "int",
            "status": "str",
            "active_version": "int",
            "started_by": "str?",
            "started_at": "time",
            "seq": "int",
        },
    ),
    Table(
        "workflow_versions",
        key=("workflow", "version"),
        columns={
            "workflow": "str",
            "version": "int",
            "config": "json",
            "config_hash": "str",
            "activated_by": "str?",
            "activated_at": "time",
            "seq": "int",
        },
    ),
    # One row per item of the dataset that entered the workflow.
    # status: in_progress, done (it has its final-label record) or excluded (it may have a
    # final-label record too, kept as history, when it was excluded after finalization).
    # closed_seq: when the item stopped moving (finalized, or excluded while in progress).
    Table(
        "workflow_items",
        key=("workflow", "item"),
        columns={
            "workflow": "str",
            "item": "str",
            "status": "str",
            "entered_at": "time",
            "entered_seq": "int",
            "closed_seq": "int?",
        },
    ),
    # One row per arrival of an item at a stage, admitted or not.
    Table(
        "admissions",
        key=("workflow", "stage", "item"),
        columns={
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "source": "str",
            "admitted": "bool",
            "draw": "float?",
            "threshold": "float",
            "labels_draw": "float?",
            "n_slots": "int",
            "config_version": "int",
            "at": "time",
            "seq": "int",
            "completed": "bool",
            "outputs": "json",
            "completed_seq": "int?",
        },
    ),
    # status: open, claimed, filled or cancelled.
    # claimed_roles: every role the claimant held at the moment of the claim (a list), kept
    # while the slot is claimed or filled; None while it is open or once it is cancelled.
    Table(
        "slots",
        key=("slot",),
        columns={
            "slot": "int",
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "index": "int",
            "reserved_for": "str?",
            "status": "str",
            "claimed_by": "str?",
            "claimed_at": "time?",
            "claimed_roles": "json?",
            "label": "int?",
            "config_version": "int",
            "admitted_seq": "int",
            "closed_seq": "int?",
        },
    ),
    Table(
        "releases",
        key=("seq",),
        columns={
            "seq": "int",
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "slot": "int",
            "user": "str",
            "reason": "str",
            "at": "time",
        },
    ),
    # roles: the roles the labeler held when submitting. roles_at_claim: the roles they held
    # when they claimed the slot (the ones eligibility was checked against; a role revoked
    # between the claim and the submit shows up only here).
    Table(
        "labels",
        key=("label",),
        columns={
            "label": "int",
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "slot": "int",
            "user": "str",
            "roles": "json",
            "roles_at_claim": "json?",
            "config_version": "int",
            "value": "json",
            "shown_content_hash": "str",
            "shown_prior_ids": "json",
            "show_prior": "bool",
            "shown_consensus_rule": "str?",
            "label_task": "str",
            "ontology_version": "str",
            "duration_ms": "int?",
            "claimed_at": "time?",
            "submitted_at": "time",
            "seq": "int",
        },
        unique=(("workflow", "item", "user"), ("slot",)),
    ),
    # key_status: scored, no_key (the item lacks the property) or invalid_key.
    Table(
        "scores",
        key=("label",),
        columns={
            "label": "int",
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "user": "str",
            "key_property": "str",
            "key_status": "str",
            "agreed": "bool?",
            "rule_version": "str?",
            "detail": "json",
            "seq": "int",
        },
    ),
    Table(
        "consensus",
        key=("workflow", "stage", "item"),
        columns={
            "workflow": "str",
            "stage": "str",
            "item": "str",
            "label_ids": "json",
            "agreed": "bool",
            "agreed_label": "json",
            "rule_version": "str",
            "detail": "json",
            "config_version": "int",
            "at": "time",
            "seq": "int",
        },
    ),
    # status: final, unresolved or none.
    Table(
        "final_labels",
        key=("workflow", "item"),
        columns={
            "workflow": "str",
            "item": "str",
            "status": "str",
            "value": "json",
            "source_stage": "str?",
            "label_ids": "json",
            "rule": "str",
            "config_version": "int",
            "at": "time",
            "seq": "int",
        },
    ),
    # Pause state, one row per workflow or stage that was ever paused. A pause stops claims
    # only. paused is the current state; by, at, reason and seq describe the latest pause or
    # resume. The two are independent: resuming a workflow leaves its paused stages paused.
    Table(
        "workflow_pauses",
        key=("workflow",),
        columns={
            "workflow": "str",
            "paused": "bool",
            "by": "str?",
            "at": "time",
            "reason": "str",
            "seq": "int",
        },
    ),
    Table(
        "stage_pauses",
        key=("workflow", "stage"),
        columns={
            "workflow": "str",
            "stage": "str",
            "paused": "bool",
            "by": "str?",
            "at": "time",
            "reason": "str",
            "seq": "int",
        },
    ),
    # The last id handed out for slots and labels, so new ids continue the sequence.
    Table("counters", key=("name",), columns={"name": "str", "value": "int"}),
)


class Tables(Protocol):
    """The table interface the reducer and the engine use.

    ``get`` and ``select`` return fresh copies; changing a returned row changes nothing.
    ``select`` returns rows in primary-key order.
    """

    def get(self, table: str, /, **key: Scalar) -> Row | None:
        """The row with this primary key, or None."""
        ...

    def insert(self, table: str, row: Mapping[str, Any], /) -> None:
        """Add a row; it must have every column. Raises IntegrityError on a duplicate key."""
        ...

    def update(self, table: str, key: Mapping[str, Scalar], changes: Mapping[str, Any], /) -> None:
        """Change some non-key columns of an existing row."""
        ...

    def select(self, table: str, /, **where: Scalar) -> list[Row]:
        """The rows whose columns equal the given values, in primary-key order."""
        ...


def _copy_json(value: Any) -> Any:
    """A deep copy of a JSON value (faster than ``copy.deepcopy`` for plain data)."""
    if isinstance(value, dict):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_copy_json(v) for v in value]
    return value


def _copy_row(row: Mapping[str, Any]) -> Row:
    return {k: _copy_json(v) if isinstance(v, dict | list) else v for k, v in row.items()}


def check_value(table: Table, column: str, value: Any) -> Any:
    """Check a value against its column type; return it as stored (ints in float columns
    become floats, as SQL would store them)."""
    kind, nullable = table.column_type(column)
    if value is None:
        if nullable or kind == "json":
            return None
        raise IntegrityError(f"{table.name}.{column} may not be None.")
    if kind in ("str", "time"):
        ok = isinstance(value, str)
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "json":
        ok = isinstance(value, dict | list | str | int | float | bool)
    else:  # int or float
        number = int if kind == "int" else int | float
        ok = isinstance(value, number) and not isinstance(value, bool)
    if not ok:
        raise IntegrityError(f"{table.name}.{column} is {kind}, not {type(value).__name__}.")
    return float(value) if kind == "float" else value


class InMemoryTables:
    """The table interface over Python dicts, with an index on every scalar column and an
    undo journal so a failed transaction leaves no trace."""

    def __init__(self, schema: tuple[Table, ...] = SCHEMA):
        self.schema = {table.name: table for table in schema}
        self._rows: dict[str, dict[tuple, Row]] = {name: {} for name in self.schema}
        self._index: dict[str, dict[str, defaultdict[Any, set[tuple]]]] = {
            name: {
                column: defaultdict(set)
                for column in table.columns
                if table.column_type(column)[0] != "json"
            }
            for name, table in self.schema.items()
        }
        self._journal: list[tuple[str, str, tuple, Row | None]] | None = None

    # --- transactions (used by the store) ---------------------------------------------

    def begin(self) -> None:
        self._journal = []

    def commit(self) -> None:
        self._journal = None

    def rollback(self) -> None:
        journal, self._journal = self._journal or [], None
        for _action, table, key, old in reversed(journal):
            self._put(table, key, old)

    # --- the Tables interface ---------------------------------------------------------

    def get(self, table: str, /, **key: Scalar) -> Row | None:
        spec = self._table(table)
        row = self._rows[table].get(self._key(spec, key))
        return None if row is None else _copy_row(row)

    def insert(self, table: str, row: Mapping[str, Any], /) -> None:
        spec = self._table(table)
        if set(row) != set(spec.columns):
            missing = sorted(set(spec.columns) - set(row))
            extra = sorted(set(row) - set(spec.columns))
            raise IntegrityError(f"Row for {table}: missing {missing}, unknown {extra}.")
        stored = {column: check_value(spec, column, row[column]) for column in spec.columns}
        key = tuple(stored[column] for column in spec.key)
        if key in self._rows[table]:
            raise IntegrityError(f"{table} already has a row with key {key}.")
        self._check_unique(spec, key, stored)
        self._journal_and_put(table, key, _copy_row(stored))

    def update(self, table: str, key: Mapping[str, Scalar], changes: Mapping[str, Any], /) -> None:
        spec = self._table(table)
        row_key = self._key(spec, key)
        old = self._rows[table].get(row_key)
        if old is None:
            raise IntegrityError(f"{table} has no row with key {row_key}.")
        for column in changes:
            if column not in spec.columns or column in spec.key:
                raise IntegrityError(f"{table}.{column} cannot be updated.")
        new = dict(old)
        new.update({column: check_value(spec, column, value) for column, value in changes.items()})
        self._check_unique(spec, row_key, new)
        self._journal_and_put(table, row_key, _copy_row(new))

    def select(self, table: str, /, **where: Scalar) -> list[Row]:
        self._table(table)
        rows = self._rows[table]
        index = self._index[table]
        for column in where:
            if column not in index:
                raise IntegrityError(f"Cannot filter {table} on {column}.")
        if where:
            candidates = min((index[c].get(v, set()) for c, v in where.items()), key=len)
            keys = [k for k in candidates if all(rows[k][c] == v for c, v in where.items())]
        else:
            keys = list(rows)
        return [_copy_row(rows[k]) for k in sorted(keys)]

    def snapshot(self) -> dict[str, list[Row]]:
        """Every table's rows in primary-key order, as plain data that compares with ==."""
        return {
            name: [_copy_row(rows[k]) for k in sorted(rows)] for name, rows in self._rows.items()
        }

    # --- internals --------------------------------------------------------------------

    def _table(self, table: str) -> Table:
        try:
            return self.schema[table]
        except KeyError:
            raise IntegrityError(f"There is no table named {table}.") from None

    def _check_unique(self, spec: Table, key: tuple, row: Row) -> None:
        index = self._index[spec.name]
        for columns in spec.unique:
            same = set.intersection(*(index[c].get(row[c], set()) for c in columns)) - {key}
            if same:
                values = {c: row[c] for c in columns}
                raise IntegrityError(f"{spec.name} already has a row with {values}.")

    @staticmethod
    def _key(spec: Table, key: Mapping[str, Scalar]) -> tuple:
        if set(key) != set(spec.key):
            raise IntegrityError(f"The key of {spec.name} is {spec.key}, not {tuple(key)}.")
        return tuple(key[column] for column in spec.key)

    def _journal_and_put(self, table: str, key: tuple, row: Row) -> None:
        if self._journal is not None:
            self._journal.append(("put", table, key, self._rows[table].get(key)))
        self._put(table, key, row)

    def _put(self, table: str, key: tuple, row: Row | None) -> None:
        """Set (or, with None, remove) a row and keep the indexes in step."""
        rows, index = self._rows[table], self._index[table]
        old = rows.pop(key, None)
        if old is not None:
            for column, values in index.items():
                values[old[column]].discard(key)
        if row is not None:
            rows[key] = row
            for column, values in index.items():
                values[row[column]].add(key)
