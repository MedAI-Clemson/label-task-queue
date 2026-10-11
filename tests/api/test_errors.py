"""Every engine error becomes the right HTTP status, with the engine's own words as detail."""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from labelflow.api.errors import ApiError, Forbidden, NotLoggedIn, Unprocessable, status_for
from labelflow.engine import errors as engine_errors
from labelflow.labeltask import UnknownLabelTask

EXPECTED = {
    "UnknownRole": 404,
    "UnknownDataset": 404,
    "UnknownItem": 404,
    "UnknownWorkflow": 404,
    "UnknownStage": 404,
    "UnknownSlot": 404,
    "UnknownLabelTaskRef": 404,
    "NotFound": 404,
    "AlreadyExists": 409,
    "VersionConflict": 409,
    "NotAllowed": 409,
    "NotEligible": 403,
    "SlotNotYours": 403,
    "EditRefused": 422,
    "InvalidRequest": 422,
    "InvalidLabel": 422,
    "InvalidShown": 422,
    "BrokenChain": 500,
    "EngineError": 422,
}


def make_error(kind: type[Exception]) -> Exception:
    if kind.__name__ in ("EditRefused", "InvalidLabel"):
        return kind(["first problem", "second problem"])
    return kind(f"A plain message from {kind.__name__}.")


ENGINE_ERRORS = [
    kind
    for _, kind in inspect.getmembers(engine_errors, inspect.isclass)
    if issubclass(kind, engine_errors.EngineError)
]


def test_every_engine_error_has_an_expected_status():
    assert {kind.__name__ for kind in ENGINE_ERRORS} == set(EXPECTED)


@pytest.mark.parametrize("kind", ENGINE_ERRORS, ids=lambda kind: kind.__name__)
def test_the_status_of_an_engine_error(kind):
    assert status_for(make_error(kind)) == EXPECTED[kind.__name__]


@pytest.fixture
def raising(app, client):
    """``raising(error)``: the response when a request makes the app raise ``error``."""
    quiet = TestClient(app, raise_server_exceptions=False)

    def call(error: Exception):
        def boom() -> None:
            raise error

        path = f"/boom/{id(error)}"
        app.add_api_route(path, boom)
        return quiet.get(path)

    return call


@pytest.mark.parametrize("kind", ENGINE_ERRORS, ids=lambda kind: kind.__name__)
def test_an_engine_error_gives_its_status_and_its_own_words(raising, kind):
    error = make_error(kind)
    response = raising(error)
    assert response.status_code == EXPECTED[kind.__name__]
    body = response.json()
    assert body["detail"] == str(error)
    assert body["problems"] == getattr(error, "problems", [])


def test_a_refused_edit_and_a_rejected_label_list_every_problem(raising):
    for kind in (engine_errors.EditRefused, engine_errors.InvalidLabel):
        body = raising(kind(["first problem", "second problem"])).json()
        assert body["problems"] == ["first problem", "second problem"]
        assert "first problem" in body["detail"]
        assert "second problem" in body["detail"]


def test_api_errors_keep_their_status_and_problems(raising):
    assert raising(Forbidden("No.")).status_code == 403
    unprocessable = raising(Unprocessable("Bad.", ["one", "two"]))
    assert (unprocessable.status_code, unprocessable.json()) == (
        422,
        {"detail": "Bad.", "problems": ["one", "two"]},
    )
    login = raising(NotLoggedIn("Log in."))
    assert login.status_code == 401
    assert login.headers["www-authenticate"] == "Bearer"
    assert ApiError("x").status == 400


def test_a_label_task_the_server_lacks_is_a_server_problem(raising):
    response = raising(UnknownLabelTask("image_ontology@2", ["image_ontology@1"]))
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("No label task named 'image_ontology@2' is registered.")
    assert "Did you mean 'image_ontology@1'?" in detail
    assert "does not have this label task installed" in detail


def test_an_unexpected_error_says_little(raising):
    response = raising(RuntimeError("secret: /srv/db/labelflow.db"))
    assert response.status_code == 500
    assert response.json() == {
        "detail": "Something went wrong on the server. The details are in the server log.",
        "problems": [],
    }


def test_a_request_with_several_mistakes_lists_them(admin):
    response = admin.post("/accounts", {"display_name": 3, "is_admin": "maybe"})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "The request is not valid: 4 problems."
    assert "username: Field required" in body["problems"]
    assert "password: Field required" in body["problems"]
    assert any(p.startswith("display_name: ") for p in body["problems"])
    assert any(p.startswith("is_admin: ") for p in body["problems"])


def test_a_request_that_is_not_json(admin):
    response = admin.post(
        "/accounts", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"].startswith("The request is not valid")


def test_an_unknown_address(client):
    response = client.get("/nothing/here")
    assert response.status_code == 404
    assert "detail" in response.json()


# The mappings, met through the real endpoints.


@pytest.mark.parametrize(
    ("status", "make_request"),
    [
        (404, lambda a, w: a.post(f"{w}/items/nope/exclude", {"reason": "x"})),  # UnknownItem
        (404, lambda a, w: a.get(f"{w}/slots/999")),  # UnknownSlot (the engine's wording)
        (404, lambda a, w: a.post(f"{w}/stages/nope/cancel-open-slots", {"reason": "x"})),
        (409, lambda a, w: a.post(f"{w.rsplit('/workflows', 1)[0]}/roles", {"role": "expert"})),
        (409, lambda a, w: a.post(f"{w}/edits", {"config": "x", "base_version": 7})),  # stale
        (422, lambda a, w: a.post(f"{w.rsplit('/workflows', 1)[0]}/roles", {"role": "1 bad"})),
    ],
)
def test_the_mappings_through_the_endpoints(admin, workflow, status, make_request):
    response = make_request(admin, workflow)
    assert response.status_code == status, response.text
    assert response.json()["detail"]
