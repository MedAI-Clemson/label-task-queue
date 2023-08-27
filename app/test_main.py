from fastapi.testclient import TestClient

from .main import app

client = TestClient(app)

import os

print(os.getcwd())


#
# Dataset
#
def test_create_dataset():
    db_json = {
        "name": "Test Dataset",
        "description": "A dataset used for testing the labelq app.",
    }
    response = client.post("/datasets/", json=db_json)
    assert response.status_code == 200


def test_get_datasets():
    response = client.get("/datasets/")
    assert response.status_code == 200
