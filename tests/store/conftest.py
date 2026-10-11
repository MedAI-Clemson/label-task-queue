"""Fixtures for the store tests: database files, engines, and a small scripted project."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from labelflow.engine import Engine, Store
from labelflow.labeltask import LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask
from labelflow.store import SqlStore, connect, create_schema


class FakeClock:
    """A clock that moves one minute forward each time it is read."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current, self.now = self.now, self.now + timedelta(minutes=1)
        return current


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'labelflow.db'}"


@pytest.fixture
def engine(url: str) -> Iterator[sa.Engine]:
    """A database with every table created, closed at the end of the test."""
    engine = connect(url, synchronous="OFF")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def open_engine(url: str, engine: sa.Engine) -> Iterator[Callable[..., sa.Engine]]:
    """Another engine on the same database file (as another process or thread would have)."""
    made: list[sa.Engine] = []

    def open_another(**options: Any) -> sa.Engine:
        made.append(connect(url, **options))
        return made[-1]

    yield open_another
    for other in made:
        other.dispose()


@pytest.fixture
def store(engine: sa.Engine) -> SqlStore:
    return SqlStore(engine, "demo")


ANNOTATE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}
CONFIG = {
    "workflow": "wf",
    "dataset": "ds",
    "label_task": "standin@1",
    "stages": [ANNOTATE, ADJUDICATE],
    "final": ["adjudicate", "annotate"],
}
PEOPLE = {"ana": "annotator", "ben": "annotator", "dr_smith": "expert"}


def make_engine(
    store: Store,
    workflow: str = "wf",
    task: StandInLabelTask | None = None,
    items: int = 3,
    config: dict[str, Any] | None = None,
) -> Engine:
    """An engine with a deterministic clock and a small project: roles, people, a dataset
    of ``items`` items and a started workflow (by default the two-stage ``CONFIG``)."""
    engine = Engine(store, LabelTaskRegistry([task or StandInLabelTask()]), clock=FakeClock())
    for role in ("annotator", "expert"):
        engine.define_role(role, "admin")
    for user, role in PEOPLE.items():
        engine.grant_role(user, role, "admin", "setup")
    engine.create_dataset("ds", "admin")
    for i in range(1, items + 1):
        engine.import_item(
            "ds", f"img_{i}", f"file:///img_{i}.png", f"sha-{i}", {"truth": i % 2}, "admin"
        )
    engine.start_workflow({**(config or CONFIG), "workflow": workflow}, 1234, "admin")
    return engine


def work(engine: Engine, workflow: str, user: str, value: str) -> int | None:
    """Ask for work as ``user`` and submit ``{"value": value}``; the slot, or None."""
    slot = engine.request_work(workflow, user)
    if slot is None:
        return None
    packet = engine.work_packet(workflow, slot.slot, user)
    engine.submit(workflow, slot.slot, user, {"value": value}, packet.shown())
    return slot.slot


def run_script(engine: Engine, workflow: str = "wf", pauses: bool = True) -> None:
    """Commands that touch most tables: a claim is released, an item is excluded, two
    labelers disagree on some items and an expert adjudicates one. With ``pauses``, the
    workflow and a stage are paused and resumed on the way (filling the pause tables)."""
    slot = engine.request_work(workflow, "ana")
    engine.release(workflow, slot.slot, "ana", "changed my mind")
    engine.exclude_item(workflow, "img_3", "admin", "unreadable")
    if pauses:
        engine.pause_workflow(workflow, "admin", "checking the scanner")
        engine.pause_stage(workflow, "annotate", "admin", "new guidelines")
        engine.resume_workflow(workflow, "admin", "scanner is fine")
        engine.resume_stage(workflow, "annotate", "admin", "guidelines are out")
    for value in ("cat", "dog", "cat"):
        work(engine, workflow, "ana", value)
        work(engine, workflow, "ben", "cat")
    work(engine, workflow, "dr_smith", "dog")
    engine.grant_role("cy", "annotator", "admin", "hired")
    engine.revoke_role("cy", "annotator", "admin", "left")


@pytest.fixture
def make_sql_engine(engine: sa.Engine) -> Callable[..., Engine]:
    """An engine over a new project in the test's database."""

    def make(project: str, workflow: str = "wf") -> Engine:
        return make_engine(SqlStore(engine, project), workflow)

    return make


def raw_insert(engine: sa.Engine, table: str, row: dict[str, Any], project: str = "demo") -> None:
    """INSERT with plain SQL, bypassing SqlStore, so only the database's own rules apply.
    JSON values are written as text."""
    row = {"project": project, **row}
    columns = ", ".join(f'"{c}"' for c in row)
    marks = ", ".join("?" for _ in row)
    values = tuple(json.dumps(v) if isinstance(v, dict | list) else v for v in row.values())
    with engine.begin() as connection:
        connection.exec_driver_sql(f'INSERT INTO "{table}" ({columns}) VALUES ({marks})', values)


def raw(engine: sa.Engine, sql: str, *params: Any) -> list[tuple]:
    """Run plain SQL in a write transaction; the rows it returns, if any."""
    with engine.begin() as connection:
        result = connection.exec_driver_sql(sql, params)
        return list(result.fetchall()) if result.returns_rows else []


# Helpers are handed to tests as fixtures: test modules cannot import from a conftest.


@pytest.fixture(scope="session")
def build_engine() -> Callable[..., Engine]:
    return make_engine


@pytest.fixture(scope="session")
def do_work() -> Callable[..., int | None]:
    return work


@pytest.fixture(scope="session")
def script() -> Callable[..., None]:
    return run_script


@pytest.fixture(scope="session")
def sql() -> Callable[..., list[tuple]]:
    return raw


@pytest.fixture(scope="session")
def sql_insert() -> Callable[..., None]:
    return raw_insert
