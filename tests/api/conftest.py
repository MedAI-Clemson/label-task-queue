"""Fixtures for the API tests.

Every test gets its own database file (a copy of one made by ``alembic upgrade head``, so
the migrations are what the tests run on), an app built by ``create_app``, and a clock the
test can move. People are ``Caller`` objects: a name, a login token and ``get``/``post``
methods that send the token. Helpers are given to tests as fixtures, because test modules
cannot import from a conftest.
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from labelflow.api import Settings, create_app
from labelflow.labeltask import LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask

ROOT = Path(__file__).resolve().parents[2]

# What the end-to-end tests run: annotators label every image, an expert reviews 20%.
REVIEW_SAMPLE = """\
workflow: xray_main
dataset: batch1
label_task: standin@1

stages:
  - name: annotate
    assign_to: annotator

  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2

final: [expert_review, annotate]
"""


class Clock:
    """A clock the test can move. It is the clock of the engine and of the logins."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **how_much: float) -> None:
        self.now += timedelta(**how_much)


class RecordingClient(TestClient):
    """A test client that remembers every call and its status, for the check that every
    endpoint is tested (``test_zz_every_endpoint_is_tested``)."""

    # shared by every client of the test session
    calls: ClassVar[set[tuple[str, str, int]]] = set()

    def request(self, method: str, url: Any, **kwargs: Any) -> Response:  # type: ignore[override]
        response = super().request(method, url, **kwargs)
        self.calls.add((method.upper(), urlparse(str(url)).path, response.status_code))
        return response


@dataclass
class Caller:
    """A person who is logged in (or nobody, with no token), making calls."""

    client: TestClient
    name: str = "nobody"
    token: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        auth = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return auth | self.extra_headers

    def request(self, method: str, path: str, **kwargs: Any) -> Response:
        headers = self.headers | kwargs.pop("headers", {})
        return self.client.request(method, path, headers=headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> Response:
        return self.request("POST", path, json=json, **kwargs)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database made by ``alembic upgrade head``; each test works on a copy."""
    path = tmp_path_factory.mktemp("template") / "labelflow.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """The media folder the server may read images from."""
    folder = tmp_path / "media"
    folder.mkdir()
    return folder


@pytest.fixture
def settings(tmp_path: Path, template_db: Path, media: Path) -> Settings:
    database = tmp_path / "labelflow.db"
    shutil.copy(template_db, database)
    return Settings(
        database_uri=f"sqlite:///{database}",
        label_tasks=tmp_path / "ontologies",
        media_root=media,
        session_hours=1,
        sqlite_synchronous="OFF",  # durability does not matter in tests
        password_cost=4,  # nor does the cost of hashing a password
    )


@pytest.fixture
def label_tasks() -> LabelTaskRegistry:
    return LabelTaskRegistry([StandInLabelTask()])


@pytest.fixture
def app(settings: Settings, label_tasks: LabelTaskRegistry, clock: Clock) -> FastAPI:
    return create_app(settings, label_tasks=label_tasks, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client that runs the app's startup and shutdown."""
    with RecordingClient(app) as client:
        yield client


@pytest.fixture
def app_with(
    settings: Settings, label_tasks: LabelTaskRegistry, clock: Clock, password: str
) -> Iterator[Callable[..., tuple[Caller, Caller]]]:
    """``app_with(media_root=None)``: an app built with changed settings, on the same
    database, with the admin ``root`` and the labeler ``ana`` logged in."""
    with ExitStack() as stack:

        def make(**changes: Any) -> tuple[Caller, Caller]:
            app = create_app(replace(settings, **changes), label_tasks=label_tasks, clock=clock)
            client = stack.enter_context(RecordingClient(app))
            runtime = app.state.runtime
            people = []
            for name, is_admin in (("root", True), ("ana", False)):
                runtime.accounts.create(name, password, name.title(), is_admin=is_admin)
                people.append(Caller(client, name, runtime.identity.login(name, password).token))
            return people[0], people[1]

        yield make


@pytest.fixture
def anon(client: TestClient) -> Caller:
    """Somebody who is not logged in."""
    return Caller(client)


def make_password() -> str:
    """A password made at run time; it starts with letters, so ``swapcase`` always changes it."""
    return "pw" + secrets.token_urlsafe(16)


@pytest.fixture
def password() -> str:
    """The password of the accounts ``new_person`` makes; a new one for every test."""
    return make_password()


@pytest.fixture
def new_password() -> Callable[[], str]:
    """``new_password()``: another password, different from ``password`` and from each other."""
    return make_password


@pytest.fixture
def new_person(app: FastAPI, client: TestClient, password: str) -> Callable[..., Caller]:
    """Make an account straight in the database and log in, without going through the API."""
    runtime = app.state.runtime
    default_password = password

    def make(name: str, admin: bool = False, password: str = default_password) -> Caller:
        runtime.accounts.create(name, password, name.title(), is_admin=admin, created_by="setup")
        return Caller(client, name, runtime.identity.login(name, password).token)

    return make


@pytest.fixture
def admin(new_person: Callable[..., Caller]) -> Caller:
    return new_person("root", admin=True)


@pytest.fixture
def ana(new_person: Callable[..., Caller]) -> Caller:
    return new_person("ana")


@pytest.fixture
def ben(new_person: Callable[..., Caller]) -> Caller:
    return new_person("ben")


@pytest.fixture
def dr_smith(new_person: Callable[..., Caller]) -> Caller:
    return new_person("dr_smith")


@pytest.fixture
def expect() -> Callable[..., Any]:
    """``expect(response, 201)``: check the status (showing the body if it is wrong) and
    return the JSON body."""

    def check(response: Response, status: int = 200) -> Any:
        assert response.status_code == status, f"{response.status_code}: {response.text}"
        return response.json() if response.content else None

    return check


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def make_items() -> Callable[..., list[dict[str, Any]]]:
    """Items for an import, with their content hashes: ``make_items(20)``."""

    def make(count: int, start: int = 1, **properties: Any) -> list[dict[str, Any]]:
        return [
            {
                "item": f"img_{i:03d}",
                "uri": f"https://images.example.org/img_{i:03d}.png",
                "content_hash": sha256(f"image {i}"),
                "properties": properties,
            }
            for i in range(start, start + count)
        ]

    return make


def single_stage(workflow: str, dataset: str = "batch1", **stage: Any) -> dict[str, Any]:
    """A config with one stage, ``annotate``, for annotators (``stage`` changes its keys)."""
    return {
        "workflow": workflow,
        "dataset": dataset,
        "label_task": "standin@1",
        "stages": [{"name": "annotate", "assign_to": "annotator"} | stage],
    }


@pytest.fixture
def one_stage() -> Callable[..., dict[str, Any]]:
    """``one_stage("name")``: see ``single_stage``."""
    return single_stage


@pytest.fixture
def base() -> str:
    return "/projects/xray"


@pytest.fixture
def project(
    admin: Caller,
    ana: Caller,
    ben: Caller,
    dr_smith: Caller,
    base: str,
    expect: Callable[..., Any],
    make_items: Callable[..., list[dict[str, Any]]],
) -> str:
    """The project ``xray``: roles annotator (ana, ben) and expert (dr_smith), and the
    dataset ``batch1`` with 20 images. Returns its address."""
    expect(admin.post("/projects", {"name": "xray"}), 201)
    for role in ("annotator", "expert"):
        expect(admin.post(f"{base}/roles", {"role": role}), 201)
    for person, role in ((ana, "annotator"), (ben, "annotator"), (dr_smith, "expert")):
        grant = {"user": person.name, "reason": "test setup"}
        expect(admin.post(f"{base}/roles/{role}/grant", grant))
    expect(admin.post(f"{base}/datasets", {"name": "batch1"}), 201)
    expect(admin.post(f"{base}/datasets/batch1/items", {"items": make_items(20)}), 201)
    return base


@pytest.fixture
def workflow(admin: Caller, project: str, expect: Callable[..., Any]) -> str:
    """The workflow ``xray_main`` (annotators label, an expert reviews 20%) is running with
    seed 11. Returns its address."""
    expect(admin.post(f"{project}/workflows", {"config": REVIEW_SAMPLE, "seed": 11}), 201)
    return f"{project}/workflows/xray_main"


@pytest.fixture
def label_with(expect: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """``label_with(person, workflow, "cat")``: ask for work, label it and submit; returns
    the work, or None when there is none."""

    def label(person: Caller, workflow: str, value: str = "cat") -> dict[str, Any] | None:
        response = person.post(f"{workflow}/next")
        if response.status_code == 204:
            return None
        work = expect(response)
        slot = work["slot"]["slot"]
        submit = {"label": {"value": value}, "shown": work["shown"], "duration_ms": 1200}
        expect(person.post(f"{workflow}/slots/{slot}/submit", submit), 201)
        return work

    return label


@pytest.fixture(scope="session")
def recorded_calls() -> set[tuple[str, str, int]]:
    """Every call the tests made through the ``client`` fixture: (method, path, status)."""
    return RecordingClient.calls
