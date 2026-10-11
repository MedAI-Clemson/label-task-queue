"""Rebuilding the derived tables from the log, and the command that does it."""

from __future__ import annotations

import subprocess
import sys

import pytest

from labelflow.engine import BrokenChain
from labelflow.store import SqlStore
from labelflow.store.rebuild import main


@pytest.fixture
def project(build_engine, script, store):
    """A store with a scripted project in it, and a neighbor that must stay untouched."""
    script(build_engine(store))
    return store


def damage(engine, sql) -> None:
    """Make the derived tables disagree with the log, as a bug or a manual edit would."""
    sql(engine, "DELETE FROM labels WHERE label = 1")
    sql(engine, "UPDATE final_labels SET status = 'unresolved'")
    sql(engine, "INSERT INTO counters VALUES ('demo', 'ghost', 5)")


def test_rebuilding_gives_identical_tables(project):
    before = project.snapshot()
    report = project.rebuild_derived()
    assert project.snapshot() == before
    assert report.identical
    assert report.events == len(project.events())
    assert report.rows == sum(len(rows) for rows in before.values())


def test_rebuilding_repairs_damaged_tables(engine, project, sql):
    good = project.snapshot()
    damage(engine, sql)
    assert project.snapshot() != good
    report = project.rebuild_derived()
    assert project.snapshot() == good
    text = "\n".join(report.differences)
    assert "labels (1,)" in text and "final_labels" in text and "counters ('ghost',)" in text


def test_rebuilding_one_project_leaves_the_others_alone(engine, project, make_sql_engine, script):
    other = make_sql_engine("other")
    script(other)
    others = other.store.snapshot(), other.store.events()
    project.rebuild_derived()
    assert (other.store.snapshot(), other.store.events()) == others


def test_verifying_reports_differences_and_changes_nothing(engine, project, sql):
    assert project.verify_derived().identical
    damage(engine, sql)
    damaged = project.snapshot()
    report = project.verify_derived()
    assert not report.identical
    assert any(
        "labels (1,): the log produces it, but the tables lack it" in d for d in report.differences
    )
    assert any(
        "final_labels" in d and "'unresolved' in the tables" in d for d in report.differences
    )
    assert any(
        "counters ('ghost',): in the tables, but the log does not produce it" in d
        for d in report.differences
    )
    assert project.snapshot() == damaged


def test_a_long_list_of_differences_is_shortened(engine, project, sql):
    sql(engine, "DELETE FROM slots")
    report = project.verify_derived()
    lines = [line for line in report.differences if line.startswith("slots")]
    assert len(lines) == 6 and lines[-1].startswith("slots: and ")


def test_a_broken_chain_stops_a_rebuild_and_changes_nothing(engine, project, sql):
    sql(engine, "DROP TRIGGER events_no_update")
    sql(engine, "UPDATE events SET event = REPLACE(event, 'setup', 'forged') WHERE seq = 4")
    before = project.snapshot()
    with pytest.raises(BrokenChain, match="Event 4 was changed"):
        project.rebuild_derived()
    with pytest.raises(BrokenChain, match="Event 4 was changed"):
        project.verify_derived()
    assert project.snapshot() == before


def test_a_changed_column_is_noticed_even_though_the_json_is_intact(engine, project, sql):
    """The columns are what ``events(workflow=...)`` filters on, so they are checked too."""
    sql(engine, "DROP TRIGGER events_no_update")
    sql(engine, "UPDATE events SET actor = 'mallory' WHERE seq = 2")
    with pytest.raises(BrokenChain, match=r"Event 2 was changed: its column actor is 'mallory'"):
        project.verify_derived()


def test_a_removed_event_breaks_the_chain(engine, project, sql):
    sql(engine, "DROP TRIGGER events_no_delete")
    sql(engine, "DELETE FROM events WHERE seq = 5")
    with pytest.raises(BrokenChain, match="Event 6 is out of place"):
        project.rebuild_derived()


# --- the command --------------------------------------------------------------------------


def test_the_command_rebuilds(url, engine, project, sql, capsys):
    damage(engine, sql)
    assert main([url, "--project", "demo"]) == 0
    out = capsys.readouterr().out
    assert f"Project 'demo': {len(project.events())} events" in out
    assert "fingerprint chain verified; rebuilt" in out
    assert "corrected these differences" in out and "counters ('ghost',)" in out
    assert project.verify_derived().identical


def test_the_command_says_when_nothing_was_wrong(url, project, capsys):
    assert main([url, "--project", "demo"]) == 0
    assert "nothing changed" in capsys.readouterr().out


def test_the_command_can_only_verify(url, engine, project, sql, capsys):
    assert main([url, "--project", "demo", "--verify-only"]) == 0
    assert "a fresh replay matches" in capsys.readouterr().out
    damage(engine, sql)
    damaged = project.snapshot()
    assert main([url, "--project", "demo", "--verify-only"]) == 1
    out = capsys.readouterr().out
    assert "differ from the event log" in out and "labels (1,)" in out
    assert project.snapshot() == damaged  # verifying changed nothing
    assert main([url, "--project", "demo"]) == 0  # rebuilding does
    assert project.snapshot() != damaged


def test_the_command_refuses_a_damaged_log(url, engine, project, sql, capsys):
    sql(engine, "DROP TRIGGER events_no_update")
    sql(engine, "UPDATE events SET event = REPLACE(event, 'setup', 'forged') WHERE seq = 4")
    before = project.snapshot()
    for extra in ([], ["--verify-only"]):
        assert main([url, "--project", "demo", *extra]) == 2
        err = capsys.readouterr().err
        assert "damaged" in err and "Event 4 was changed" in err
    assert project.snapshot() == before


def test_the_command_names_the_projects_when_given_a_wrong_one(url, project, capsys):
    assert main([url, "--project", "dmeo"]) == 2
    err = capsys.readouterr().err
    assert "project 'dmeo' has no events" in err and "Projects here: demo" in err


def test_the_command_reports_a_missing_database(tmp_path, capsys):
    missing = tmp_path / "nothing.db"
    assert main([f"sqlite:///{missing}", "--project", "demo"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()
    assert main(["postgresql://localhost/labelflow", "--project", "demo"]) == 2
    assert "SQLite" in capsys.readouterr().err


def test_the_command_runs_as_a_module(url, project):
    run = subprocess.run(
        [sys.executable, "-m", "labelflow.store.rebuild", url, "--project", "demo"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("Project 'demo':")
    assert SqlStore(project.engine, "demo").verify_derived().identical
