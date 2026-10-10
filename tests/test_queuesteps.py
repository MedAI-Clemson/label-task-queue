import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    create_labelqueue,
    create_queuestep,
    create_task,
    ok,
    queuestep_json,
    setup_queue,
)


def test_create_queuestep_default_policy_args(client: TestClient):
    create_labelqueue(client, "Q")
    data = ok(
        client.post(
            "/labelqueues/1/queue_step/",
            json={"name": "Step 1", "num_records": 5, "type": "distribute"},
        )
    )
    assert data == {
        "id": 1,
        "name": "Step 1",
        "description": None,
        "num_records": 5,
        "type": "distribute",
        "policy_args": {"random": False},
        "num_records_completed": 0,
        "rank": 1,
        "completed": False,
        "labelqueue": {"id": 1, "name": "Q", "description": None},
    }


def test_create_queuestep_null_policy_args_uses_default(client: TestClient):
    create_labelqueue(client)
    data = create_queuestep(client, 1, policy_args=None)
    assert data["policy_args"] == {"random": False}


def test_create_queuestep_explicit_policy_args(client: TestClient):
    create_labelqueue(client)
    data = create_queuestep(client, 1, policy_args={"random": True})
    assert data["policy_args"] == {"random": True}
    assert ok(client.get("/queuesteps/1"))["policy_args"] == {"random": True}


def test_create_queuestep_rejects_extra_policy_args(client: TestClient):
    create_labelqueue(client)
    response = client.post(
        "/labelqueues/1/queue_step/",
        json=queuestep_json(policy_args={"random": True, "unknown": 1}),
    )
    assert response.status_code == 422
    assert ok(client.get("/labelqueues/1"))["queuesteps"] == []


def test_create_queuestep_rejects_invalid_policy_args(client: TestClient):
    create_labelqueue(client)
    response = client.post(
        "/labelqueues/1/queue_step/",
        json=queuestep_json(policy_args={"random": "not a bool"}),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("queue_type", ["consensus", "priority"])
def test_create_queuestep_unimplemented_policy_type(client: TestClient, queue_type):
    create_labelqueue(client)
    response = client.post(
        "/labelqueues/1/queue_step/", json=queuestep_json(type=queue_type)
    )
    assert response.status_code == 422
    assert "has not been implemented" in response.json()["detail"][0]["msg"]
    assert ok(client.get("/labelqueues/1"))["queuesteps"] == []


def test_create_queuestep_unknown_policy_type(client: TestClient):
    create_labelqueue(client)
    response = client.post("/labelqueues/1/queue_step/", json=queuestep_json(type="x"))
    assert response.status_code == 422


@pytest.mark.parametrize("num_records", [0, -1])
def test_create_queuestep_num_records_must_be_positive(client: TestClient, num_records):
    create_labelqueue(client)
    response = client.post(
        "/labelqueues/1/queue_step/", json=queuestep_json(num_records=num_records)
    )
    assert response.status_code == 422


def test_create_queuestep_missing_fields(client: TestClient):
    create_labelqueue(client)
    response = client.post("/labelqueues/1/queue_step/", json={"name": "Step"})
    assert response.status_code == 422


def test_create_queuestep_labelqueue_not_found(client: TestClient):
    response = client.post("/labelqueues/1/queue_step/", json=queuestep_json())
    assert response.status_code == 404


def test_queuestep_rank_per_labelqueue(client: TestClient):
    create_labelqueue(client, "A")
    create_labelqueue(client, "B")

    assert [create_queuestep(client, 1)["rank"] for _ in range(3)] == [1, 2, 3]
    # ranks are assigned per labelqueue, so the second labelqueue starts at 1 again
    assert [create_queuestep(client, 2)["rank"] for _ in range(2)] == [1, 2]

    steps = ok(client.get("/labelqueues/1"))["queuesteps"]
    assert sorted(s["rank"] for s in steps) == [1, 2, 3]


def test_queuestep_rank_after_delete(client: TestClient):
    create_labelqueue(client)
    create_queuestep(client, 1)
    create_queuestep(client, 1)
    ok(client.delete("/queuesteps/1"))
    # new steps are always added after the current last step
    assert create_queuestep(client, 1)["rank"] == 3


def test_get_queuestep(client: TestClient):
    create_labelqueue(client, "Q")
    create_queuestep(client, 1, name="S")
    data = ok(client.get("/queuesteps/1"))
    assert data["name"] == "S"
    assert data["labelqueue"]["name"] == "Q"


def test_get_queuestep_not_found(client: TestClient):
    response = client.get("/queuesteps/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "QueueStep not found"


def test_update_queuestep(client: TestClient):
    create_labelqueue(client)
    create_queuestep(client, 1, name="S", num_records=3, policy_args={"random": True})

    data = ok(client.patch("/queuesteps/1", json={"name": "Renamed", "num_records": 7}))
    assert data["name"] == "Renamed"
    assert data["num_records"] == 7
    # untouched fields are retained
    assert data["policy_args"] == {"random": True}
    assert data["rank"] == 1


def test_update_queuestep_policy_args(client: TestClient):
    create_labelqueue(client)
    create_queuestep(client, 1)

    data = ok(client.patch("/queuesteps/1", json={"policy_args": {"random": True}}))
    assert data["policy_args"] == {"random": True}
    assert ok(client.get("/queuesteps/1"))["policy_args"] == {"random": True}

    # setting policy_args to null restores the defaults for the step type
    data = ok(client.patch("/queuesteps/1", json={"policy_args": None}))
    assert data["policy_args"] == {"random": False}


@pytest.mark.parametrize(
    "body",
    [
        {"policy_args": {"unknown": 1}},
        {"type": "consensus"},
        {"type": None},
        {"num_records": 0},
        {"name": None},
    ],
)
def test_update_queuestep_invalid(client: TestClient, body):
    create_labelqueue(client)
    original = create_queuestep(client, 1)

    response = client.patch("/queuesteps/1", json=body)
    assert response.status_code == 422

    assert ok(client.get("/queuesteps/1")) == original


def test_update_queuestep_not_found(client: TestClient):
    assert client.patch("/queuesteps/1", json={"name": "x"}).status_code == 404


def test_delete_queuestep(client: TestClient):
    create_labelqueue(client)
    create_queuestep(client, 1)
    assert ok(client.delete("/queuesteps/1")) == {"ok": True}
    assert client.get("/queuesteps/1").status_code == 404
    assert ok(client.get("/labelqueues/1"))["queuesteps"] == []


def test_delete_queuestep_not_found(client: TestClient):
    assert client.delete("/queuesteps/1").status_code == 404


def test_delete_queuestep_with_tasks_conflict(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    response = client.delete(f"/queuesteps/{ids['queuestep_id']}")
    assert response.status_code == 409
    assert client.get(f"/queuesteps/{ids['queuestep_id']}").status_code == 200
