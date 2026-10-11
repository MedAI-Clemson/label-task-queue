"""Settings from the environment, and what happens at startup."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from labelflow.api import Settings, create_app
from labelflow.api import app as module_app
from labelflow.labeltask import OntologyError

ROOT = Path(__file__).resolve().parents[2]
KB, MB = 1024, 1024 * 1024


def test_the_defaults():
    settings = Settings.from_env({})
    assert settings.database_uri == "sqlite:///./db/labelflow.db"
    assert settings.label_tasks == Path("./ontologies")
    assert settings.media_root is None
    assert settings.session_lifetime == timedelta(hours=12)
    assert settings.cookie_secure is False
    assert settings.max_request_bytes == 20 * MB
    assert settings.max_import_items == 10_000
    assert settings.max_properties_bytes == 256 * KB
    assert settings.max_label_bytes == 4 * MB
    assert settings.sqlite_synchronous == "FULL"
    assert settings.password_cost == 14


def test_everything_can_be_set_from_the_environment():
    settings = Settings.from_env(
        {
            "DATABASE_URI": "sqlite:////data/labelflow.db",
            "LABELFLOW_LABEL_TASKS": "/etc/labelflow/ontologies",
            "LABELFLOW_MEDIA_ROOT": "/mnt/images",
            "LABELFLOW_SESSION_HOURS": "0.5",
            "LABELFLOW_COOKIE_SECURE": "Yes",
            "LABELFLOW_MAX_REQUEST_MB": "5",
            "LABELFLOW_MAX_IMPORT_ITEMS": "500",
            "LABELFLOW_MAX_PROPERTIES_KB": "16",
            "LABELFLOW_MAX_LABEL_KB": "1024",
            "LABELFLOW_SQLITE_SYNCHRONOUS": "NORMAL",
        }
    )
    assert settings.database_uri == "sqlite:////data/labelflow.db"
    assert settings.label_tasks == Path("/etc/labelflow/ontologies")
    assert settings.media_root == Path("/mnt/images")
    assert settings.session_lifetime == timedelta(minutes=30)
    assert settings.cookie_secure is True
    assert settings.max_request_bytes == 5 * MB
    assert settings.max_import_items == 500
    assert settings.max_properties_bytes == 16 * KB
    assert settings.max_label_bytes == 1 * MB
    assert settings.sqlite_synchronous == "NORMAL"


def test_empty_values_mean_the_default():
    settings = Settings.from_env(
        {
            "DATABASE_URI": " ",
            "LABELFLOW_MEDIA_ROOT": "",
            "LABELFLOW_SESSION_HOURS": "",
            "LABELFLOW_MAX_REQUEST_MB": " ",
        }
    )
    assert settings == Settings()


def test_the_process_environment_is_read_by_default(monkeypatch):
    monkeypatch.setenv("LABELFLOW_MEDIA_ROOT", "/somewhere")
    assert Settings.from_env().media_root == Path("/somewhere")


@pytest.mark.parametrize("hours", ["0", "-3", "soon", "nan", "inf"])
def test_a_bad_session_lifetime_is_refused(hours):
    with pytest.raises(ValueError, match="LABELFLOW_SESSION_HOURS must be a positive number"):
        Settings.from_env({"LABELFLOW_SESSION_HOURS": hours})


@pytest.mark.parametrize(
    "name",
    [
        "LABELFLOW_MAX_REQUEST_MB",
        "LABELFLOW_MAX_IMPORT_ITEMS",
        "LABELFLOW_MAX_PROPERTIES_KB",
        "LABELFLOW_MAX_LABEL_KB",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "lots"])
def test_a_bad_limit_is_refused(name, value):
    with pytest.raises(ValueError, match=f"{name} must be a positive whole number"):
        Settings.from_env({name: value})


def test_a_bad_flag_is_refused():
    with pytest.raises(ValueError, match="LABELFLOW_COOKIE_SECURE must be true or false"):
        Settings.from_env({"LABELFLOW_COOKIE_SECURE": "perhaps"})
    assert Settings.from_env({"LABELFLOW_COOKIE_SECURE": "off"}).cookie_secure is False


def test_bad_values_are_refused_when_building_settings_directly():
    with pytest.raises(ValueError, match="positive number of hours"):
        Settings(session_hours=0)
    with pytest.raises(ValueError, match="max_label_bytes must be a positive number"):
        Settings(max_label_bytes=0)


def test_the_module_level_app_is_the_one_uvicorn_serves():
    assert TestClient(module_app).get("/health").json() == {"status": "ok"}
    assert module_app.title == "label-task-queue"


def test_the_health_check_answers_without_a_login(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health", headers={"Authorization": "Bearer junk"}).status_code == 200


def test_the_health_check_needs_neither_a_login_nor_a_database(tmp_path):
    app = create_app(Settings(database_uri=f"sqlite:///{tmp_path}/none/labelflow.db"))
    response = TestClient(app).get("/health")  # no startup, so nothing is opened
    assert response.json() == {"status": "ok"}
    assert not (tmp_path / "none").exists()


def ontology_folder(settings, *names):
    folder = settings.label_tasks
    folder.mkdir()
    for name in names:
        shutil.copy(ROOT / "examples" / "ontologies" / name, folder)
    return folder


def test_startup_loads_the_ontology_folder(settings):
    ontology_folder(settings, "chest_xray.yaml")
    app = create_app(settings)  # no label tasks given: they come from the folder
    with TestClient(app):
        assert app.state.runtime.registry.refs() == ["image_ontology@1"]


def test_startup_stops_when_the_ontology_folder_is_missing(settings):
    app = create_app(settings)
    with pytest.raises(OntologyError, match="There is no folder at"), TestClient(app):
        pass


def test_startup_stops_when_an_ontology_file_is_wrong(settings):
    folder = ontology_folder(settings)
    (folder / "broken.yaml").write_text("name: broken\nversion: one\n")
    app = create_app(settings)
    with pytest.raises(OntologyError, match=r"broken\.yaml"), TestClient(app):
        pass


def test_startup_stops_when_the_database_is_not_set_up(settings, tmp_path):
    empty = replace(settings, database_uri=f"sqlite:///{tmp_path / 'empty.db'}")
    ontology_folder(empty)
    app = create_app(empty)
    with pytest.raises(RuntimeError, match="alembic upgrade head"), TestClient(app):
        pass


def test_startup_stops_when_the_database_is_out_of_date(settings, tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0001")  # before the accounts tables
    ontology_folder(settings)
    app = create_app(replace(settings, database_uri=url))
    with pytest.raises(RuntimeError, match="out of date"), TestClient(app):
        pass


def test_the_database_folder_is_made_when_it_is_missing(settings, tmp_path):
    folder = tmp_path / "var" / "db"
    ontology_folder(settings)
    app = create_app(replace(settings, database_uri=f"sqlite:///{folder / 'labelflow.db'}"))
    with pytest.raises(RuntimeError, match="not set up"), TestClient(app):
        pass
    assert folder.is_dir()


def test_the_app_starts_by_itself_on_the_first_request(app):
    client = TestClient(app)  # not used as a context manager: no startup event
    assert client.get("/auth/me").status_code == 401
    assert sa.inspect(app.state.runtime.db).has_table("accounts")
    app.state.runtime.close()


def test_the_engine_of_a_project_uses_the_clock_and_the_label_tasks(app, client, clock):
    engine = app.state.runtime.engine_for("anything")
    assert engine.clock is clock
    assert engine.label_tasks.refs() == ["standin@1"]
