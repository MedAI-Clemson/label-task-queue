from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main
from app.main import app as fastapi_app


def test_lifespan_creates_tables(monkeypatch: pytest.MonkeyPatch):
    create_db_and_tables = Mock()
    monkeypatch.setattr(app.main, "create_db_and_tables", create_db_and_tables)

    with TestClient(fastapi_app):
        create_db_and_tables.assert_called_once()


def test_openapi_schema(client: TestClient):
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert {
        "/datasets/",
        "/datasets/{dataset_id}",
        "/datasets/{dataset_id}/records/",
        "/datasets/{dataset_id}/labelqueues/{labelqueue_id}",
        "/records/{record_id}",
        "/users/",
        "/users/{user_id}",
        "/queuesteps/{queuestep_id}",
        "/labelqueues/",
        "/labelqueues/{labelqueue_id}",
        "/labelqueues/{labelqueue_id}/users/{user_id}",
        "/labelqueues/{labelqueue_id}/queue_step/",
        "/labelqueues/{labelqueue_id}/{user_id}/task/",
    } == paths
