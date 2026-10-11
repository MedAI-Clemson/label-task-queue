"""``python -m labelflow.api.admin create-admin``: the first administrator."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from labelflow.api import admin as cli
from labelflow.api import create_app


def env_for(settings, **more):
    return {
        "DATABASE_URI": settings.database_uri,
        "LABELFLOW_ADMIN_PASSWORD": "a long password",
    } | more


def login(settings, label_tasks, username, password):
    """Log in through a new app on the same database; the response."""
    with TestClient(create_app(settings, label_tasks=label_tasks)) as client:
        return client.post("/auth/login", json={"username": username, "password": password})


def test_the_password_can_come_from_the_environment(settings, label_tasks, capsys):
    status = cli.main(["create-admin", "boss", "--display-name", "The Boss"], env_for(settings))
    assert status == 0
    assert capsys.readouterr().out == "Created the administrator account 'boss'.\n"
    response = login(settings, label_tasks, "boss", "a long password")
    assert response.status_code == 200
    assert response.json()["user"] == {
        "user_id": "boss",
        "display_name": "The Boss",
        "is_admin": True,
    }


def test_the_password_is_asked_for_twice_when_not_in_the_environment(settings, label_tasks):
    answers = iter(["typed password", "typed password"])
    env = {"DATABASE_URI": settings.database_uri}
    assert cli.main(["create-admin", "boss"], env, prompt=lambda _: next(answers)) == 0
    assert login(settings, label_tasks, "boss", "typed password").status_code == 200


def test_two_different_passwords_are_refused(settings, capsys):
    answers = iter(["one password", "another one"])
    env = {"DATABASE_URI": settings.database_uri}
    assert cli.main(["create-admin", "boss"], env, prompt=lambda _: next(answers)) == 1
    assert capsys.readouterr().err == "Error: The two passwords are not the same.\n"


def test_no_password_at_all_is_refused(settings, capsys):
    def no_terminal(_):
        raise EOFError

    status = cli.main(
        ["create-admin", "boss"], {"DATABASE_URI": settings.database_uri}, no_terminal
    )
    assert status == 1
    assert "Set LABELFLOW_ADMIN_PASSWORD" in capsys.readouterr().err


def test_an_existing_account_is_not_replaced(settings, label_tasks, capsys):
    assert cli.main(["create-admin", "boss"], env_for(settings)) == 0
    capsys.readouterr()
    assert (
        cli.main(
            ["create-admin", "boss"], env_for(settings, LABELFLOW_ADMIN_PASSWORD="another one")
        )
        == 1
    )
    assert capsys.readouterr().err == "Error: An account named 'boss' already exists.\n"
    assert login(settings, label_tasks, "boss", "a long password").status_code == 200


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("Boss", "a long password", "is not a valid username"),
        ("boss", "short", "The password must be 8 to 256 characters long."),
    ],
)
def test_a_bad_username_or_password_is_refused(settings, capsys, username, password, message):
    env = env_for(settings, LABELFLOW_ADMIN_PASSWORD=password)
    assert cli.main(["create-admin", username], env) == 1
    assert message in capsys.readouterr().err


def test_the_database_must_be_set_up_first(tmp_path, capsys):
    env = {
        "DATABASE_URI": f"sqlite:///{tmp_path / 'new.db'}",
        "LABELFLOW_ADMIN_PASSWORD": "a long password",
    }
    assert cli.main(["create-admin", "boss"], env) == 1
    assert "alembic upgrade head" in capsys.readouterr().err


def test_a_bad_setting_is_reported(settings, capsys):
    env = env_for(settings, LABELFLOW_SESSION_HOURS="soon")
    assert cli.main(["create-admin", "boss"], env) == 1
    assert "LABELFLOW_SESSION_HOURS" in capsys.readouterr().err


def test_a_command_is_needed(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main([])
    assert stop.value.code == 2
    assert "create-admin" in capsys.readouterr().err


def test_it_runs_as_a_module(settings, label_tasks):
    run = subprocess.run(
        [sys.executable, "-m", "labelflow.api.admin", "create-admin", "boss"],
        env=os.environ | env_for(settings),
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "Created the administrator account 'boss'.\n"
    assert login(settings, label_tasks, "boss", "a long password").status_code == 200


def test_the_first_admin_has_no_creator(settings, label_tasks, capsys):
    cli.main(["create-admin", "boss"], env_for(settings))
    with TestClient(create_app(settings, label_tasks=label_tasks)) as client:
        token = client.post(
            "/auth/login", json={"username": "boss", "password": "a long password"}
        ).json()["token"]
        (account,) = client.get("/accounts", headers={"Authorization": f"Bearer {token}"}).json()
    assert account["created_by"] is None
