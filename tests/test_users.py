from fastapi.testclient import TestClient

from tests.helpers import (
    create_labelqueue,
    create_task,
    create_user,
    ok,
    register_user,
    setup_queue,
)

USER = {"name": "Alice", "email": "alice@example.com", "role": "Labeler"}


def test_create_user(client: TestClient):
    data = ok(client.post("/users/", json=USER))
    assert data == {**USER, "id": 1, "labelqueues": []}


def test_create_admin(client: TestClient):
    data = ok(client.post("/users/", json={**USER, "role": "Admin"}))
    assert data["role"] == "Admin"


def test_create_user_invalid_email(client: TestClient):
    response = client.post("/users/", json={**USER, "email": "not-an-email"})
    assert response.status_code == 422


def test_create_user_invalid_role(client: TestClient):
    response = client.post("/users/", json={**USER, "role": "Owner"})
    assert response.status_code == 422


def test_create_user_duplicate_email(client: TestClient):
    ok(client.post("/users/", json=USER))
    response = client.post("/users/", json={**USER, "name": "Other"})
    assert response.status_code == 400

    # the session is still usable after the failed insert
    data = ok(client.post("/users/", json={**USER, "email": "bob@example.com"}))
    assert data["email"] == "bob@example.com"
    assert len(ok(client.get("/users/"))) == 2


def test_get_users(client: TestClient):
    assert ok(client.get("/users/")) == []
    create_user(client, "Alice")
    create_user(client, "Bob")
    assert [u["name"] for u in ok(client.get("/users/"))] == ["Alice", "Bob"]


def test_get_user(client: TestClient):
    create_user(client)
    create_labelqueue(client)
    register_user(client, 1, 1)

    data = ok(client.get("/users/1"))
    assert data["name"] == "Alice"
    assert [lq["id"] for lq in data["labelqueues"]] == [1]


def test_get_user_not_found(client: TestClient):
    response = client.get("/users/1")
    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_update_user(client: TestClient):
    create_user(client)
    data = ok(client.patch("/users/1", json={"name": "Alicia", "role": "Admin"}))
    assert data["name"] == "Alicia"
    assert data["role"] == "Admin"
    assert data["email"] == "alice@example.com"


def test_update_user_not_found(client: TestClient):
    assert client.patch("/users/1", json={"name": "x"}).status_code == 404


def test_update_user_rejects_null_and_invalid_values(client: TestClient):
    create_user(client)
    assert client.patch("/users/1", json={"name": None}).status_code == 422
    assert client.patch("/users/1", json={"email": "not-an-email"}).status_code == 422
    assert client.patch("/users/1", json={"role": "Owner"}).status_code == 422
    assert ok(client.get("/users/1"))["name"] == "Alice"


def test_update_user_duplicate_email(client: TestClient):
    create_user(client, "Alice")
    create_user(client, "Bob")
    response = client.patch("/users/2", json={"email": "alice@example.com"})
    assert response.status_code == 400
    assert ok(client.get("/users/2"))["email"] == "bob@example.com"


def test_delete_user(client: TestClient):
    create_user(client)
    assert ok(client.delete("/users/1")) == {"ok": True}
    assert client.get("/users/1").status_code == 404


def test_delete_user_not_found(client: TestClient):
    assert client.delete("/users/1").status_code == 404


def test_delete_user_removes_labelqueue_membership(client: TestClient):
    create_user(client)
    create_labelqueue(client)
    register_user(client, 1, 1)

    ok(client.delete("/users/1"))

    assert ok(client.get("/labelqueues/1"))["users"] == []


def test_delete_user_with_tasks_conflict(client: TestClient):
    ids = setup_queue(client)
    ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    response = client.delete(f"/users/{ids['user_id']}")
    assert response.status_code == 409
    assert client.get(f"/users/{ids['user_id']}").status_code == 200
