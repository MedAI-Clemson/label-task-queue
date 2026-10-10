from fastapi.testclient import TestClient

from tests.helpers import (
    create_labelqueue,
    create_queuestep,
    create_task,
    create_user,
    ok,
    register_user,
    setup_queue,
)


def test_create_labelqueue(client: TestClient):
    data = ok(client.post("/labelqueues/", json={"name": "Q", "description": "desc"}))
    assert data == {
        "id": 1,
        "name": "Q",
        "description": "desc",
        "dataset": None,
        "users": [],
        "queuesteps": [],
        "tasks": [],
    }


def test_create_labelqueue_requires_name(client: TestClient):
    assert client.post("/labelqueues/", json={}).status_code == 422


def test_get_labelqueues(client: TestClient):
    assert ok(client.get("/labelqueues/")) == []
    create_labelqueue(client, "A")
    create_labelqueue(client, "B")
    assert [lq["name"] for lq in ok(client.get("/labelqueues/"))] == ["A", "B"]


def test_get_labelqueue_with_relations(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    data = ok(client.get(f"/labelqueues/{ids['labelqueue_id']}"))
    assert data["dataset"]["id"] == ids["dataset_id"]
    assert [u["id"] for u in data["users"]] == [ids["user_id"]]
    assert [qs["id"] for qs in data["queuesteps"]] == [ids["queuestep_id"]]
    assert len(data["tasks"]) == 1


def test_get_labelqueue_not_found(client: TestClient):
    response = client.get("/labelqueues/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_update_labelqueue(client: TestClient):
    create_labelqueue(client, "Q", description="desc")
    data = ok(client.patch("/labelqueues/1", json={"name": "Renamed"}))
    assert data["name"] == "Renamed"
    assert data["description"] == "desc"


def test_update_labelqueue_not_found(client: TestClient):
    assert client.patch("/labelqueues/1", json={"name": "x"}).status_code == 404


def test_update_labelqueue_rejects_null_name(client: TestClient):
    create_labelqueue(client)
    assert client.patch("/labelqueues/1", json={"name": None}).status_code == 422


def test_delete_labelqueue(client: TestClient):
    create_labelqueue(client)
    assert ok(client.delete("/labelqueues/1")) == {"ok": True}
    assert client.get("/labelqueues/1").status_code == 404


def test_delete_labelqueue_not_found(client: TestClient):
    assert client.delete("/labelqueues/1").status_code == 404


def test_delete_labelqueue_deletes_queuesteps_keeps_users(client: TestClient):
    create_labelqueue(client)
    create_user(client)
    register_user(client, 1, 1)
    create_queuestep(client, 1)

    ok(client.delete("/labelqueues/1"))

    assert client.get("/queuesteps/1").status_code == 404
    assert ok(client.get("/users/1"))["labelqueues"] == []


def test_delete_labelqueue_with_tasks_conflict(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    response = client.delete(f"/labelqueues/{ids['labelqueue_id']}")
    assert response.status_code == 409
    assert client.get(f"/labelqueues/{ids['labelqueue_id']}").status_code == 200


#
# register / unregister users
#
def test_register_user(client: TestClient):
    create_labelqueue(client)
    create_user(client, "Alice")
    create_user(client, "Bob")

    data = register_user(client, 1, 1)
    assert [u["id"] for u in data["users"]] == [1]
    data = register_user(client, 1, 2)
    assert [u["id"] for u in data["users"]] == [1, 2]

    assert [lq["id"] for lq in ok(client.get("/users/1"))["labelqueues"]] == [1]


def test_register_user_labelqueue_not_found(client: TestClient):
    create_user(client)
    response = client.post("/labelqueues/1/users/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_register_user_not_found(client: TestClient):
    create_labelqueue(client)
    response = client.post("/labelqueues/1/users/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_register_user_twice(client: TestClient):
    create_labelqueue(client)
    create_user(client)
    register_user(client, 1, 1)

    response = client.post("/labelqueues/1/users/1")
    assert response.status_code == 406
    assert len(ok(client.get("/labelqueues/1"))["users"]) == 1


def test_unregister_user(client: TestClient):
    create_labelqueue(client)
    create_user(client, "Alice")
    create_user(client, "Bob")
    register_user(client, 1, 1)
    register_user(client, 1, 2)

    data = ok(client.delete("/labelqueues/1/users/1"))
    assert [u["id"] for u in data["users"]] == [2]
    assert ok(client.get("/users/1"))["labelqueues"] == []
    # the user itself still exists
    assert client.get("/users/1").status_code == 200


def test_unregister_user_labelqueue_not_found(client: TestClient):
    create_user(client)
    response = client.delete("/labelqueues/1/users/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_unregister_user_not_found(client: TestClient):
    create_labelqueue(client)
    response = client.delete("/labelqueues/1/users/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_unregister_user_not_registered(client: TestClient):
    create_labelqueue(client)
    create_user(client)
    response = client.delete("/labelqueues/1/users/1")
    assert response.status_code == 406
