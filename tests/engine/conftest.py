"""Shared helpers for the engine tests: a small project, configs, and a labeler's round.

``build`` makes an engine on a store of the kind the test is parametrized with (see the
root conftest); the helpers are handed to tests as fixtures, since test modules cannot
import from a conftest.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from labelflow.engine import Engine, InMemoryStore, Store
from labelflow.engine.simulate import FixedClock
from labelflow.labeltask import LabelTask, LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask

DEFAULT_GRANTS = {
    "ana": ("annotator",),
    "ben": ("annotator",),
    "dr_jones": ("expert",),
    "dr_smith": ("expert",),
}


def build_engine(
    config: Mapping[str, Any] | None = None,
    *,
    items: int = 4,
    grants: Mapping[str, tuple[str, ...]] = DEFAULT_GRANTS,
    roles: tuple[str, ...] = ("annotator", "expert", "trainee"),
    seed: int = 1234,
    properties: Callable[[int], dict] | None = None,
    task: LabelTask | None = None,
    store: Store | None = None,
    make_store: Callable[[], Store] = InMemoryStore,
) -> Engine:
    """A project with roles, people, a dataset ``ds`` of ``items`` images, and optionally a
    started workflow."""
    engine = Engine(
        make_store() if store is None else store,
        LabelTaskRegistry([task or StandInLabelTask()]),
        clock=FixedClock(),
    )
    for role in roles:
        engine.define_role(role, "admin")
    for user, held in grants.items():
        for role in held:
            engine.grant_role(user, role, "admin", "test setup")
    engine.create_dataset("ds", "admin")
    for i in range(1, items + 1):
        engine.import_item(
            "ds",
            f"img_{i:03d}",
            f"file:///data/img_{i:03d}.png",
            f"sha-{i:03d}",
            properties(i) if properties else {},
            "admin",
        )
    if config is not None:
        engine.start_workflow(config, seed, "admin")
    return engine


def make_config(*stages: Mapping[str, Any], final: Any = None, workflow: str = "wf") -> dict:
    config = {
        "workflow": workflow,
        "dataset": "ds",
        "label_task": "standin@1",
        "stages": [dict(stage) for stage in stages],
    }
    if final is not None:
        config["final"] = final
    return config


def do_work(engine: Engine, workflow: str, user: str, value: str = "cat", **kwargs: Any):
    """Ask for work as ``user`` and submit ``{"value": value}``; return (slot, label id)."""
    slot = engine.request_work(workflow, user)
    if slot is None:
        return None, None
    packet = engine.work_packet(workflow, slot.slot, user)
    label_id = engine.submit(workflow, slot.slot, user, {"value": value}, packet.shown(), **kwargs)
    return slot, label_id


@pytest.fixture
def build(make_store) -> Callable[..., Engine]:
    return functools.partial(build_engine, make_store=make_store)


@pytest.fixture(scope="session")
def cfg() -> Callable[..., dict]:
    return make_config


@pytest.fixture(scope="session")
def work() -> Callable[..., tuple]:
    return do_work
