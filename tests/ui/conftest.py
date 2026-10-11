"""Fixtures for the interface tests: an app with the chest X-ray ontology, a media folder
holding the spike's synthetic radiograph, and "browsers" (test clients with their own
cookie jar) logged in through the login form.

The project ``xray`` is set up straight through the runtime and the engine, as the API
tests do: roles annotator (ana, ben) and expert (dr_smith), the dataset ``batch1`` with three
items that all point at the same 512 x 512 image, and the review-sample workflow
``xray_main`` with a seed under which ``img_001`` is sampled for expert review.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from labelflow.api import Settings, create_app
from labelflow.engine import NewItem, draw
from labelflow.labeltask import LabelTaskRegistry, load_ontology

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "spikes" / "mask-tools" / "fixtures" / "xray_512.png"
ONTOLOGY = ROOT / "examples" / "ontologies" / "chest_xray.yaml"

REVIEW_SAMPLE = """\
workflow: xray_main
dataset: batch1
label_task: image_ontology@1

stages:
  - name: annotate
    assign_to: annotator

  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2

final: [expert_review, annotate]
"""

BLIND_REVIEW = """\
workflow: blind
dataset: batch1
label_task: image_ontology@1

stages:
  - name: annotate
    assign_to: annotator

  - name: expert_review
    input: annotate
    assign_to: expert
    show_prior: false

final: [expert_review, annotate]
"""


def seed_that_samples(item: str, stage: str = "expert_review", fraction: float = 0.2) -> int:
    """The first seed under which ``item`` is admitted to the sampled stage."""
    for seed in range(1, 10_000):
        if draw(seed, stage, item) < fraction:
            return seed
    raise AssertionError("no seed found")


SEED = seed_that_samples("img_001")


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "labelflow.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return path


@pytest.fixture
def media(tmp_path: Path) -> Path:
    folder = tmp_path / "media"
    folder.mkdir()
    shutil.copy(IMAGE, folder / "xray_512.png")
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
        sqlite_synchronous="OFF",
        password_cost=4,
    )


@pytest.fixture
def label_tasks() -> LabelTaskRegistry:
    return LabelTaskRegistry([load_ontology(ONTOLOGY)])


@pytest.fixture
def app(settings: Settings, label_tasks: LabelTaskRegistry) -> Iterator[FastAPI]:
    app = create_app(settings, label_tasks=label_tasks)
    with TestClient(app):  # runs the startup and shutdown around the test
        yield app


@pytest.fixture
def password() -> str:
    return "pw" + secrets.token_urlsafe(12)


class Browser:
    """A person at a browser: a test client with its own cookie jar."""

    def __init__(self, app: FastAPI, name: str = "nobody"):
        self.client = TestClient(app, follow_redirects=False)
        self.name = name
        self._csrf: str | None = None

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.client.get(path, **kwargs)

    def post(self, path: str, data: dict[str, Any] | None = None, **kwargs: Any) -> Response:
        """Post a form, with the page's form token unless ``csrf=False`` is given."""
        form = dict(data or {})
        if kwargs.pop("csrf", True):
            form.setdefault("csrf", self.csrf())
        return self.client.post(path, data=form, **kwargs)

    def post_json(self, path: str, json: Any) -> Response:
        return self.client.post(path, json=json)

    def login(self, username: str, password: str) -> Response:
        return self.client.post(
            "/ui/login", data={"username": username, "password": password}, follow_redirects=False
        )

    def csrf(self) -> str:
        if self._csrf is None:
            page = self.get("/ui/")
            found = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text)
            self._csrf = found.group(1) if found else ""
        return self._csrf


@pytest.fixture
def new_person(app: FastAPI, password: str) -> Callable[..., Browser]:
    """``new_person("ana")``: an account made straight in the database, logged in through
    the login form."""
    runtime = app.state.runtime

    def make(name: str, admin: bool = False) -> Browser:
        runtime.accounts.create(name, password, name.title(), is_admin=admin, created_by="setup")
        browser = Browser(app, name)
        response = browser.login(name, password)
        assert response.status_code == 303, response.text
        return browser

    return make


@pytest.fixture
def anon(app: FastAPI) -> Browser:
    return Browser(app)


@pytest.fixture
def admin(new_person: Callable[..., Browser]) -> Browser:
    return new_person("root", admin=True)


@pytest.fixture
def ana(new_person: Callable[..., Browser]) -> Browser:
    return new_person("ana")


@pytest.fixture
def ben(new_person: Callable[..., Browser]) -> Browser:
    return new_person("ben")


@pytest.fixture
def dr_smith(new_person: Callable[..., Browser]) -> Browser:
    return new_person("dr_smith")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(
    app: FastAPI, admin: Browser, ana: Browser, ben: Browser, dr_smith: Browser, media: Path
):
    """The project ``xray``: roles, three people, and ``batch1`` with three items."""
    runtime = app.state.runtime
    runtime.projects.create("xray", "root")
    engine = runtime.engine_for("xray")
    for role in ("annotator", "expert"):
        engine.define_role(role, "root")
    for person, role in (("ana", "annotator"), ("ben", "annotator"), ("dr_smith", "expert")):
        engine.grant_role(person, role, "root", "test setup")
    engine.create_dataset("batch1", "root")
    digest = sha256_of(media / "xray_512.png")
    engine.import_items(
        "batch1",
        [NewItem(item=f"img_{i:03d}", uri="xray_512.png", content_hash=digest) for i in (1, 2, 3)],
        "root",
    )
    return "xray"


@pytest.fixture
def workflow(app: FastAPI, project: str) -> str:
    """``xray_main`` is running over ``batch1`` with a seed under which img_001 is reviewed."""
    engine = app.state.runtime.engine_for(project)
    from labelflow.engine.config import validate_config

    report = validate_config(REVIEW_SAMPLE, engine.validation_context())
    assert report.config is not None, report.render()
    engine.start_workflow(report.config, SEED, "root")
    return "xray_main"


@pytest.fixture
def expect() -> Callable[..., str]:
    """``expect(response, 200)``: check the status (showing the body if it is wrong) and
    return the text."""

    def check(response: Response, status: int = 200) -> str:
        assert response.status_code == status, f"{response.status_code}: {response.text[:2000]}"
        return response.text

    return check


def empty_mask(size: int = 512) -> dict[str, Any]:
    return {"size": [size, size], "counts": [size * size]}


def make_label(view: str = "PA") -> dict[str, Any]:
    """A valid label for the chest X-ray ontology on the 512 x 512 image: a few lung
    pixels and an empty effusion mask."""
    lung = [512 * 100, 50, 462, 50, 462, 50]
    lung.append(512 * 512 - sum(lung))
    return {
        "classifications": {"view": view, "quality": "good"},
        "masks": {"lung": {"size": [512, 512], "counts": lung}, "effusion": empty_mask()},
    }


@pytest.fixture
def simple_label() -> Callable[..., dict[str, Any]]:
    """``simple_label()``: a valid label for the ontology (see ``make_label``)."""
    return make_label


@pytest.fixture
def blind_config() -> str:
    return BLIND_REVIEW
