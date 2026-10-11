"""The Alembic migrations create the same database as ``create_schema``.

The initial migration is a written-out copy of the schema. These tests fail when the tables
declared in ``labelflow.engine.tables.SCHEMA`` (or the store's constraints and triggers) change
without a new migration revision.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from labelflow.engine import SCHEMA, Table
from labelflow.store import SqlStore, connect, create_schema
from labelflow.store.schema import TRIGGERS, build_metadata

ROOT = Path(__file__).resolve().parents[2]

HOW_TO_FIX = """
To bring the migrations up to date, add a new revision (never edit one that has already
been applied to a database):
  1. DATABASE_URI=sqlite:///scratch.db alembic upgrade head
  2. DATABASE_URI=sqlite:///scratch.db alembic revision --autogenerate -m "what changed"
  3. Open the new file in src/labelflow/store/migrations/versions/ and check it. Autogenerate does
     not see triggers or the WHERE of a partial index: write those out with op.execute(...).
  4. Run this test again.
"""


@pytest.fixture
def config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False  # keep pytest's logging as it is
    return config


@pytest.fixture
def migrated(config: Config, url: str) -> Iterator[sa.Engine]:
    """A fresh database brought to the newest revision by ``alembic upgrade head``."""
    command.upgrade(config, "head")
    engine = connect(url)
    yield engine
    engine.dispose()


def schema_objects(engine: sa.Engine, kind: str) -> dict[str, str]:
    """The SQL of every index or trigger, whitespace-normalized, by name."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = ? AND sql IS NOT NULL", (kind,)
        )
        return {name: re.sub(r"\s+", " ", sql).strip() for name, sql in rows}


def describe(difference: object) -> str:
    """One short line for an Alembic difference, e.g. ``add_column roles extra``."""
    if isinstance(difference, list):  # column changes come as a list of tuples
        return "; ".join(describe(d) for d in difference)
    operation, *details = difference  # type: ignore[misc]
    named = [getattr(d, "name", None) or str(d)[:60] for d in details if d is not None]
    return " ".join([operation, *named])


def test_upgrading_creates_the_tables_the_code_expects(migrated):
    with migrated.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        differences = compare_metadata(context, build_metadata())
    assert not differences, (
        "The migrations no longer match the tables the store builds from "
        "labelflow.engine.tables.SCHEMA. Differences found by Alembic:\n  "
        + "\n  ".join(map(describe, differences))
        + "\n"
        + HOW_TO_FIX
    )


def test_a_schema_change_without_a_migration_is_noticed(migrated):
    """The guard above has teeth: a new table and a new column both show up."""
    wider = [
        replace(t, columns={**t.columns, "extra": "int?"}) if t.name == "roles" else t
        for t in SCHEMA
    ]
    changed = (*wider, Table("newtable", key=("a",), columns={"a": "int"}))
    with migrated.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, build_metadata(changed))
    assert sorted(map(describe, differences)) == ["add_column roles extra", "add_table newtable"]


def test_the_triggers_and_indexes_match_the_code(migrated, tmp_path):
    reference = connect(f"sqlite:///{tmp_path / 'reference.db'}")
    try:
        create_schema(reference)
        for kind in ("trigger", "index"):
            expected, found = schema_objects(reference, kind), schema_objects(migrated, kind)
            different = sorted(k for k in found.keys() & expected.keys() if found[k] != expected[k])
            assert found == expected, (
                f"The {kind}s created by the migrations differ from labelflow.store.schema.\n"
                f"  only in the code: {sorted(expected.keys() - found.keys())}\n"
                f"  only in the migrations: {sorted(found.keys() - expected.keys())}\n"
                f"  different SQL: {different}\n" + HOW_TO_FIX
            )
    finally:
        reference.dispose()


def test_the_migrated_database_has_the_triggers_and_the_partial_index(migrated):
    assert (
        set(schema_objects(migrated, "trigger"))
        == set(TRIGGERS)
        == {
            "events_no_update",
            "events_no_delete",
            "events_in_order",
            "slots_claim_only_when_open",
        }
    )
    index = schema_objects(migrated, "index")["uq_slots_one_claim_per_person"]
    assert "UNIQUE" in index and "WHERE status = 'claimed'" in index


def test_the_migrated_database_works_with_the_store(migrated, build_engine, script):
    store = SqlStore(migrated, "demo")
    script(build_engine(store))
    assert store.verify_derived().identical
    with pytest.raises(sa.exc.IntegrityError, match="append-only"), migrated.begin() as connection:
        connection.exec_driver_sql("DELETE FROM events")


def test_the_accounts_migration_adds_the_deployment_tables(migrated):
    inspector = sa.inspect(migrated)
    assert {"accounts", "sessions", "projects"} <= set(inspector.get_table_names())
    assert [c["name"] for c in inspector.get_columns("accounts")] == [
        "user_id",
        "display_name",
        "password_hash",
        "is_admin",
        "active",
        "created_by",
        "created_at",
    ]
    assert inspector.get_pk_constraint("sessions")["constrained_columns"] == ["token_hash"]
    assert [i["name"] for i in inspector.get_indexes("sessions")] == ["ix_sessions_user_id"]


def test_upgrading_keeps_an_existing_project(config, url, build_engine, script):
    """A project made before revisions 0002 and 0003 comes through the upgrade untouched.

    The store needs every table of the current schema, so the project is made at the head
    revision without pausing anything, and the database is taken back to 0001 (which drops
    only the tables added since) before it is upgraded again.
    """
    command.upgrade(config, "head")
    engine = connect(url)
    try:
        store = SqlStore(engine, "demo")
        script(build_engine(store), pauses=False)
        before = store.snapshot()
        engine.dispose()
        command.downgrade(config, "0001")
        tables = set(sa.inspect(engine).get_table_names())
        assert not tables & {"accounts", "sessions", "projects", "workflow_pauses", "stage_pauses"}
        assert "events" in tables
        engine.dispose()
        command.upgrade(config, "head")
        store = SqlStore(engine, "demo")
        assert store.snapshot() == before
        assert store.verify_derived().identical
    finally:
        engine.dispose()


def test_the_pauses_migration_adds_and_removes_its_two_tables(config, url):
    command.upgrade(config, "0002")
    engine = connect(url)
    try:
        assert not {"workflow_pauses", "stage_pauses"} & set(sa.inspect(engine).get_table_names())
        engine.dispose()
        command.upgrade(config, "0003")
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint("stage_pauses")["constrained_columns"] == [
            "project",
            "workflow",
            "stage",
        ]
        assert {"workflow_pauses", "stage_pauses"} <= set(inspector.get_table_names())
        engine.dispose()
        command.downgrade(config, "0002")
        assert not {"workflow_pauses", "stage_pauses"} & set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_there_is_one_head_and_it_can_be_undone(config, url, migrated):
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    migrated.dispose()
    command.downgrade(config, "base")
    engine = connect(url)
    try:
        with engine.connect() as connection:
            tables = sa.inspect(connection).get_table_names()
        assert tables == ["alembic_version"]
    finally:
        engine.dispose()


def test_the_database_url_comes_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "from_env.db"
    monkeypatch.setenv("DATABASE_URI", f"sqlite:///{path}")
    config = Config(str(ROOT / "alembic.ini"))  # no URL set on the config
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    assert path.exists()
    engine = connect(f"sqlite:///{path}")
    try:
        assert "events" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_the_default_database_is_labelflow_db_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.chdir(tmp_path)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    assert (tmp_path / "labelflow.db").exists()
