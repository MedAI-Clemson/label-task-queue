import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from main import app, get_session
from models import *

client = TestClient(app)


# get a clean db session for testing
@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# override default db with testing session
@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override

    client = TestClient(app)

    yield client

    app.dependency_overrides.clear()


#
# Dataset
#
db_json = {
    "name": "Test Dataset",
    "description": "A dataset used for testing the labelq app.",
}
db_records = [
    {"data": {"text": "A field of flowers"}},
    {"data": {"text": "A pocket full of posies"}},
    {"data": {"text": "A bird in hand is worth two in the bush"}},
    {"data": {"text": "Why in the night sky are the lights hung."}},
]


def test_create_dataset(client: TestClient):
    response = client.post("/datasets/", json=db_json)

    assert response.status_code == 200

    DatasetReadWithRelations(**response.json())


def test_get_datasets(client: TestClient):
    client.post("/datasets/", json=db_json)
    client.post("/datasets/", json=db_json)
    response = client.get("/datasets/")
    assert response.status_code == 200

    dataset_list = [DatasetReadWithRelations(**data) for data in response.json()]
    assert len(dataset_list) == 2


def test_get_dataset_by_id(client: TestClient):
    client.post("/datasets/", json=db_json)
    response = client.get("/datasets/1")
    assert response.status_code == 200

    dataset = DatasetReadWithRelations(**response.json())
    assert dataset.name == db_json["name"]
    assert dataset.description == db_json["description"]


def test_create_records(client: TestClient):
    client.post("/datasets/", json=db_json)
    response = client.post("/dataset/1/records", json=db_records)
    assert response.status_code == 200

    response = client.get("/datasets/1")
    dataset = DatasetReadWithRelations(**response.json())
    assert len(dataset.records) == len(db_records)
    assert dataset.records[2].data["text"] == db_records[2]["data"]["text"]
