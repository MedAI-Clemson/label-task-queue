"""Task assignment: POST /labelqueues/{labelqueue_id}/{user_id}/task/"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import QueueStep, QueueType
from tests.helpers import (
    RECORDS,
    create_dataset,
    create_labelqueue,
    create_queuestep,
    create_records,
    create_task,
    create_user,
    ok,
    register_dataset,
    register_user,
    setup_queue,
)


def assigned_record_ids(client: TestClient, ids: dict, count: int) -> list[int]:
    return [
        ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))["record"]["id"]
        for _ in range(count)
    ]


def assert_queue_empty(client: TestClient, ids: dict):
    response = create_task(client, ids["labelqueue_id"], ids["user_id"])
    assert response.status_code == 406
    assert response.json()["detail"] == "Queue is empty."


@contextmanager
def modify_queuestep(session: Session, queuestep_id: int):
    queuestep = session.get(QueueStep, queuestep_id)
    yield queuestep
    session.add(queuestep)
    session.commit()


def test_create_task(client: TestClient):
    ids = setup_queue(client)

    data = ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))

    assert data["id"] == 1
    assert data["completed"] is False
    assert data["completed_data"] == {}
    assert data["created_at"]
    assert data["record"] == {"id": 1, "dataset_id": ids["dataset_id"], **RECORDS[0]}
    assert data["dataset"]["id"] == ids["dataset_id"]
    assert data["user"]["id"] == ids["user_id"]
    assert data["labelqueue"]["id"] == ids["labelqueue_id"]
    assert data["queuestep"]["id"] == ids["queuestep_id"]

    tasks = ok(client.get(f"/labelqueues/{ids['labelqueue_id']}"))["tasks"]
    assert [t["id"] for t in tasks] == [1]


#
# preconditions
#
def test_create_task_labelqueue_not_found(client: TestClient):
    create_user(client)
    response = create_task(client, 1, 1)
    assert response.status_code == 404
    assert response.json()["detail"] == "LabelQueue not found"


def test_create_task_user_not_in_labelqueue(client: TestClient):
    ids = setup_queue(client)
    other = create_user(client, "Bob")

    response = create_task(client, ids["labelqueue_id"], other["id"])
    assert response.status_code == 404
    assert response.json()["detail"] == "The provided user does not belong to the labelqueue."


def test_create_task_user_does_not_exist(client: TestClient):
    ids = setup_queue(client)
    response = create_task(client, ids["labelqueue_id"], 999)
    assert response.status_code == 404


def test_create_task_no_dataset(client: TestClient):
    create_labelqueue(client)
    create_user(client)
    register_user(client, 1, 1)
    create_queuestep(client, 1)

    response = create_task(client, 1, 1)
    assert response.status_code == 406
    assert "does not have a registered dataset" in response.json()["detail"]


def test_create_task_empty_dataset(client: TestClient):
    ids = setup_queue(client, records=[])
    response = create_task(client, ids["labelqueue_id"], ids["user_id"])
    assert response.status_code == 406
    assert "does not have any records" in response.json()["detail"]


def test_create_task_no_queuestep(client: TestClient):
    create_dataset(client)
    create_records(client, 1)
    create_labelqueue(client)
    create_user(client)
    register_user(client, 1, 1)
    register_dataset(client, 1, 1)

    response = create_task(client, 1, 1)
    assert response.status_code == 406
    assert "does not have an active queue step" in response.json()["detail"]


def test_create_task_all_queuesteps_completed(client: TestClient, session: Session):
    ids = setup_queue(client)
    with modify_queuestep(session, ids["queuestep_id"]) as queuestep:
        queuestep.completed = True

    response = create_task(client, ids["labelqueue_id"], ids["user_id"])
    assert response.status_code == 406
    assert "does not have an active queue step" in response.json()["detail"]


def test_create_task_unimplemented_policy(client: TestClient, session: Session):
    # unimplemented policies cannot be created through the API, so change the type directly
    ids = setup_queue(client)
    with modify_queuestep(session, ids["queuestep_id"]) as queuestep:
        queuestep.type = QueueType.consensus

    response = create_task(client, ids["labelqueue_id"], ids["user_id"])
    assert response.status_code == 406
    assert "NotImplementedError" in response.json()["detail"]
    assert ok(client.get(f"/labelqueues/{ids['labelqueue_id']}"))["tasks"] == []


#
# distribute policy
#
def test_distribute_sequential_order(client: TestClient):
    ids = setup_queue(client, step={"policy_args": {"random": False}})

    assert assigned_record_ids(client, ids, len(RECORDS)) == [1, 2, 3, 4]
    assert_queue_empty(client, ids)


def test_distribute_sequential_skips_deleted_records(client: TestClient):
    ids = setup_queue(client)
    ok(client.delete("/records/2"))
    assert assigned_record_ids(client, ids, 3) == [1, 3, 4]
    assert_queue_empty(client, ids)


def test_distribute_random_assigns_each_record_once(client: TestClient):
    ids = setup_queue(client, step={"policy_args": {"random": True}})

    record_ids = assigned_record_ids(client, ids, len(RECORDS))
    assert sorted(record_ids) == [1, 2, 3, 4]
    assert_queue_empty(client, ids)


def test_distribute_random_uses_random_choice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    # make the "random" choice deterministic: always pick the largest remaining id
    monkeypatch.setattr("app.models.random.choice", max)
    ids = setup_queue(client, step={"policy_args": {"random": True}})

    assert assigned_record_ids(client, ids, len(RECORDS)) == [4, 3, 2, 1]


def test_distribute_tasks_shared_between_users(client: TestClient):
    ids = setup_queue(client)
    bob = create_user(client, "Bob")
    register_user(client, ids["labelqueue_id"], bob["id"])

    first = ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))
    second = ok(create_task(client, ids["labelqueue_id"], bob["id"]))

    assert (first["record"]["id"], first["user"]["id"]) == (1, ids["user_id"])
    assert (second["record"]["id"], second["user"]["id"]) == (2, bob["id"])


def test_distribute_respects_queuestep_capacity(client: TestClient):
    ids = setup_queue(client, step={"num_records": 2})

    assert assigned_record_ids(client, ids, 2) == [1, 2]
    # records remain, but the step has handed out num_records tasks
    assert_queue_empty(client, ids)
    assert len(ok(client.get(f"/labelqueues/{ids['labelqueue_id']}"))["tasks"]) == 2


def test_distribute_capacity_increase(client: TestClient):
    ids = setup_queue(client, step={"num_records": 1})
    assert assigned_record_ids(client, ids, 1) == [1]
    assert_queue_empty(client, ids)

    ok(client.patch(f"/queuesteps/{ids['queuestep_id']}", json={"num_records": 2}))
    assert assigned_record_ids(client, ids, 1) == [2]
    assert_queue_empty(client, ids)


def test_distribute_queue_exhausted(client: TestClient):
    ids = setup_queue(client, records=RECORDS[:2], step={"num_records": 10})

    assert assigned_record_ids(client, ids, 2) == [1, 2]
    assert_queue_empty(client, ids)
    # still empty on repeated requests
    assert_queue_empty(client, ids)


def test_active_queuestep_is_lowest_rank_incomplete(client: TestClient, session: Session):
    ids = setup_queue(client, step={"num_records": 1})
    second = create_queuestep(client, ids["labelqueue_id"], num_records=10)

    task = ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))
    assert task["queuestep"]["id"] == ids["queuestep_id"]

    with modify_queuestep(session, ids["queuestep_id"]) as queuestep:
        queuestep.completed = True

    task = ok(create_task(client, ids["labelqueue_id"], ids["user_id"]))
    assert task["queuestep"]["id"] == second["id"]
    # records already assigned by an earlier step of the labelqueue are not reassigned
    assert task["record"]["id"] == 2
