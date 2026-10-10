"""Small helpers that create objects through the API."""

from fastapi.testclient import TestClient

DATASET = {
    "name": "Test Dataset",
    "description": "A dataset used for testing the labelq app.",
}
RECORDS = [
    {"data": {"text": "A field of flowers"}},
    {"data": {"text": "A pocket full of posies"}},
    {"data": {"text": "A bird in hand is worth two in the bush"}},
    {"data": {"text": "Why in the night sky are the lights hung."}},
]


def ok(response, status_code: int = 200) -> dict:
    assert response.status_code == status_code, response.text
    return response.json()


def create_dataset(client: TestClient, **fields) -> dict:
    return ok(client.post("/datasets/", json={**DATASET, **fields}))


def create_records(client: TestClient, dataset_id: int, records=RECORDS) -> None:
    ok(client.post(f"/datasets/{dataset_id}/records/", json=list(records)))


def create_user(client: TestClient, name="Alice", email=None, role="Labeler") -> dict:
    email = email or f"{name.lower()}@example.com"
    return ok(client.post("/users/", json={"name": name, "email": email, "role": role}))


def create_labelqueue(client: TestClient, name="Test Queue", **fields) -> dict:
    return ok(client.post("/labelqueues/", json={"name": name, **fields}))


def queuestep_json(**fields) -> dict:
    return {"name": "Step", "num_records": 10, "type": "distribute", **fields}


def create_queuestep(client: TestClient, labelqueue_id: int, **fields) -> dict:
    return ok(
        client.post(
            f"/labelqueues/{labelqueue_id}/queue_step/", json=queuestep_json(**fields)
        )
    )


def register_user(client: TestClient, labelqueue_id: int, user_id: int) -> dict:
    return ok(client.post(f"/labelqueues/{labelqueue_id}/users/{user_id}"))


def register_dataset(client: TestClient, labelqueue_id: int, dataset_id: int) -> None:
    ok(client.post(f"/datasets/{dataset_id}/labelqueues/{labelqueue_id}"))


def setup_queue(client: TestClient, records=RECORDS, step: dict | None = None) -> dict:
    """
    Create a dataset with records, a user and a labelqueue with one distribute step,
    all wired together. Returns the created ids.
    """
    dataset = create_dataset(client)
    if records:
        create_records(client, dataset["id"], records)
    user = create_user(client)
    labelqueue = create_labelqueue(client)
    register_user(client, labelqueue["id"], user["id"])
    register_dataset(client, labelqueue["id"], dataset["id"])
    queuestep = create_queuestep(client, labelqueue["id"], **(step or {}))
    return {
        "dataset_id": dataset["id"],
        "user_id": user["id"],
        "labelqueue_id": labelqueue["id"],
        "queuestep_id": queuestep["id"],
    }


def create_task(client: TestClient, labelqueue_id: int, user_id: int):
    return client.post(f"/labelqueues/{labelqueue_id}/{user_id}/task/")
