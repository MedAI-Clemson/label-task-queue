from fastapi.testclient import TestClient

from app.models import DatasetReadWithRelations
from tests.helpers import (
    DATASET,
    RECORDS,
    create_dataset,
    create_labelqueue,
    create_records,
    create_task,
    ok,
    register_dataset,
    setup_queue,
)


def test_create_dataset(client: TestClient):
    data = ok(client.post("/datasets/", json=DATASET))

    dataset = DatasetReadWithRelations(**data)
    assert dataset.id == 1
    assert dataset.name == DATASET["name"]
    assert dataset.description == DATASET["description"]
    assert dataset.records == []
    assert dataset.labelqueues == []


def test_create_dataset_description_optional(client: TestClient):
    data = ok(client.post("/datasets/", json={"name": "No description"}))
    assert data["description"] is None


def test_create_dataset_requires_name(client: TestClient):
    response = client.post("/datasets/", json={"description": "no name"})
    assert response.status_code == 422


def test_get_datasets(client: TestClient):
    assert ok(client.get("/datasets/")) == []

    create_dataset(client)
    create_dataset(client, name="Second")
    data = ok(client.get("/datasets/"))

    datasets = [DatasetReadWithRelations(**item) for item in data]
    assert [d.name for d in datasets] == [DATASET["name"], "Second"]


def test_get_dataset_by_id(client: TestClient):
    create_dataset(client)
    dataset = DatasetReadWithRelations(**ok(client.get("/datasets/1")))
    assert dataset.name == DATASET["name"]
    assert dataset.description == DATASET["description"]


def test_get_dataset_not_found(client: TestClient):
    response = client.get("/datasets/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Dataset not found"


def test_create_records(client: TestClient):
    create_dataset(client)
    assert ok(client.post("/datasets/1/records/", json=RECORDS)) == {"ok": True}

    dataset = DatasetReadWithRelations(**ok(client.get("/datasets/1")))
    assert len(dataset.records) == len(RECORDS)
    assert [r.id for r in dataset.records] == [1, 2, 3, 4]
    assert dataset.records[2].data["text"] == RECORDS[2]["data"]["text"]


def test_create_records_appends(client: TestClient):
    create_dataset(client)
    create_records(client, 1, RECORDS[:2])
    create_records(client, 1, RECORDS[2:])
    assert len(ok(client.get("/datasets/1"))["records"]) == len(RECORDS)


def test_create_records_dataset_not_found(client: TestClient):
    response = client.post("/datasets/1/records/", json=RECORDS)
    assert response.status_code == 404
    assert ok(client.get("/datasets/")) == []


def test_update_dataset(client: TestClient):
    create_dataset(client)
    data = ok(client.patch("/datasets/1", json={"name": "Renamed"}))
    assert data["name"] == "Renamed"
    # fields not in the request are unchanged
    assert data["description"] == DATASET["description"]

    data = ok(client.patch("/datasets/1", json={"description": None}))
    assert data["name"] == "Renamed"
    assert data["description"] is None


def test_update_dataset_not_found(client: TestClient):
    response = client.patch("/datasets/1", json={"name": "Renamed"})
    assert response.status_code == 404


def test_update_dataset_rejects_null_name(client: TestClient):
    create_dataset(client)
    response = client.patch("/datasets/1", json={"name": None})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]
    assert ok(client.get("/datasets/1"))["name"] == DATASET["name"]


def test_delete_dataset(client: TestClient):
    create_dataset(client)
    assert ok(client.delete("/datasets/1")) == {"ok": True}
    assert client.get("/datasets/1").status_code == 404


def test_delete_dataset_not_found(client: TestClient):
    assert client.delete("/datasets/1").status_code == 404


def test_delete_dataset_deletes_records(client: TestClient):
    create_dataset(client)
    create_records(client, 1)
    ok(client.delete("/datasets/1"))
    assert client.get("/records/1").status_code == 404


def test_delete_dataset_unregisters_labelqueues(client: TestClient):
    create_dataset(client)
    create_labelqueue(client)
    register_dataset(client, 1, 1)

    ok(client.delete("/datasets/1"))

    assert ok(client.get("/labelqueues/1"))["dataset"] is None


def test_delete_dataset_with_tasks_conflict(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    response = client.delete(f"/datasets/{ids['dataset_id']}")
    assert response.status_code == 409
    assert client.get(f"/datasets/{ids['dataset_id']}").status_code == 200


#
# register / unregister a dataset with a labelqueue
#
def test_register_dataset(client: TestClient):
    create_dataset(client)
    create_labelqueue(client)

    assert ok(client.post("/datasets/1/labelqueues/1")) == {"ok": True}

    assert ok(client.get("/labelqueues/1"))["dataset"]["id"] == 1
    assert [lq["id"] for lq in ok(client.get("/datasets/1"))["labelqueues"]] == [1]


def test_register_dataset_to_multiple_labelqueues(client: TestClient):
    create_dataset(client)
    create_labelqueue(client)
    create_labelqueue(client, name="Second")
    register_dataset(client, 1, 1)
    register_dataset(client, 2, 1)
    assert [lq["id"] for lq in ok(client.get("/datasets/1"))["labelqueues"]] == [1, 2]


def test_register_dataset_labelqueue_not_found(client: TestClient):
    create_dataset(client)
    response = client.post("/datasets/1/labelqueues/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_register_dataset_not_found(client: TestClient):
    create_labelqueue(client)
    response = client.post("/datasets/1/labelqueues/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Dataset not found"


def test_register_dataset_labelqueue_already_has_dataset(client: TestClient):
    create_dataset(client)
    create_dataset(client, name="Second")
    create_labelqueue(client)
    register_dataset(client, 1, 1)

    response = client.post("/datasets/2/labelqueues/1")
    assert response.status_code == 406
    assert ok(client.get("/labelqueues/1"))["dataset"]["id"] == 1


def test_unregister_dataset(client: TestClient):
    create_dataset(client)
    create_labelqueue(client)
    register_dataset(client, 1, 1)

    assert ok(client.delete("/datasets/1/labelqueues/1")) == {"ok": True}
    assert ok(client.get("/labelqueues/1"))["dataset"] is None
    assert ok(client.get("/datasets/1"))["labelqueues"] == []


def test_unregister_dataset_then_register_another(client: TestClient):
    create_dataset(client)
    create_dataset(client, name="Second")
    create_labelqueue(client)
    register_dataset(client, 1, 1)
    ok(client.delete("/datasets/1/labelqueues/1"))
    register_dataset(client, 1, 2)
    assert ok(client.get("/labelqueues/1"))["dataset"]["id"] == 2


def test_unregister_dataset_retains_tasks(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    ok(client.delete(f"/datasets/{ids['dataset_id']}/labelqueues/{ids['labelqueue_id']}"))

    assert len(ok(client.get(f"/labelqueues/{ids['labelqueue_id']}"))["tasks"]) == 1


def test_unregister_dataset_labelqueue_not_found(client: TestClient):
    create_dataset(client)
    response = client.delete("/datasets/1/labelqueues/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_unregister_dataset_not_found(client: TestClient):
    create_labelqueue(client)
    response = client.delete("/datasets/1/labelqueues/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Dataset not found"


def test_unregister_dataset_none_registered(client: TestClient):
    create_dataset(client)
    create_labelqueue(client)
    response = client.delete("/datasets/1/labelqueues/1")
    assert response.status_code == 406


def test_unregister_dataset_different_dataset_registered(client: TestClient):
    create_dataset(client)
    create_dataset(client, name="Second")
    create_labelqueue(client)
    register_dataset(client, 1, 1)

    response = client.delete("/datasets/2/labelqueues/1")
    assert response.status_code == 406
    assert ok(client.get("/labelqueues/1"))["dataset"]["id"] == 1
