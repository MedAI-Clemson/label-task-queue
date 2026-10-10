from fastapi.testclient import TestClient

from tests.helpers import (
    DATASET,
    RECORDS,
    create_dataset,
    create_records,
    create_task,
    ok,
    setup_queue,
)


def test_get_record(client: TestClient):
    create_dataset(client)
    create_records(client, 1)

    data = ok(client.get("/records/2"))
    assert data == {
        "id": 2,
        "data": RECORDS[1]["data"],
        "dataset": {"id": 1, **DATASET},
    }


def test_get_record_not_found(client: TestClient):
    response = client.get("/records/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Record not found"


def test_update_record(client: TestClient):
    create_dataset(client)
    create_records(client, 1)

    data = ok(client.patch("/records/1", json={"data": {"text": "changed"}}))
    assert data["data"] == {"text": "changed"}
    assert ok(client.get("/records/1"))["data"] == {"text": "changed"}
    # other records are untouched
    assert ok(client.get("/records/2"))["data"] == RECORDS[1]["data"]


def test_update_record_empty_body_is_noop(client: TestClient):
    create_dataset(client)
    create_records(client, 1)
    data = ok(client.patch("/records/1", json={}))
    assert data["data"] == RECORDS[0]["data"]


def test_update_record_not_found(client: TestClient):
    response = client.patch("/records/1", json={"data": {}})
    assert response.status_code == 404


def test_update_record_rejects_null_data(client: TestClient):
    create_dataset(client)
    create_records(client, 1)
    response = client.patch("/records/1", json={"data": None})
    assert response.status_code == 422


def test_delete_record(client: TestClient):
    create_dataset(client)
    create_records(client, 1)

    assert ok(client.delete("/records/1")) == {"ok": True}
    assert client.get("/records/1").status_code == 404
    assert len(ok(client.get("/datasets/1"))["records"]) == len(RECORDS) - 1


def test_delete_record_not_found(client: TestClient):
    assert client.delete("/records/1").status_code == 404


def test_delete_record_with_tasks_conflict(client: TestClient):
    ids = setup_queue(client)
    task = ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    response = client.delete(f"/records/{task['record']['id']}")
    assert response.status_code == 409
    assert client.get(f"/records/{task['record']['id']}").status_code == 200
