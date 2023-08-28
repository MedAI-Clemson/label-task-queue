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


def test_create_dataset(client: TestClient):
    response = client.post("/datasets/", json=db_json)

    assert response.status_code == 200

    DatasetReadWithRelations(**response.json())


def test_get_datasets(client: TestClient):
    response = client.get("/datasets/")
    assert response.status_code == 200

    [DatasetReadWithRelations(**data) for data in response.json()]


def test_get_dataset_by_id(client: TestClient):
    client.post("/datasets/", json=db_json)
    response = client.get("/datasets/1")
    assert response.status_code == 200

    DatasetReadWithRelations(**response.json())
