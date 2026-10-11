"""Shared helpers for the engine tests: a deterministic clock, a small project, configs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ltq.engine import Engine, InMemoryStore
from ltq.labeltask import LabelTaskRegistry
from ltq.labeltask.standin import StandInLabelTask

DEFAULT_GRANTS = {
    "ana": ("annotator",),
    "ben": ("annotator",),
    "dr_jones": ("expert",),
    "dr_smith": ("expert",),
}


class FakeClock:
    """A clock that moves one minute forward each time it is read."""

    def __init__(self, start: datetime = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)):
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(minutes=1)
        return current


def build_engine(
    config: Mapping[str, Any] | None = None,
    *,
    items: int = 4,
    grants: Mapping[str, tuple[str, ...]] = DEFAULT_GRANTS,
    roles: tuple[str, ...] = ("annotator", "expert", "trainee"),
    seed: int = 1234,
    properties: Callable[[int], dict] | None = None,
    task: StandInLabelTask | None = None,
) -> Engine:
    """A project with roles, people, a dataset ``ds`` of ``items`` images, and optionally a
    started workflow."""
    engine = Engine(
        InMemoryStore(), LabelTaskRegistry([task or StandInLabelTask()]), clock=FakeClock()
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


@pytest.fixture(scope="session")
def build() -> Callable[..., Engine]:
    return build_engine


@pytest.fixture(scope="session")
def cfg() -> Callable[..., dict]:
    return make_config


@pytest.fixture(scope="session")
def work() -> Callable[..., tuple]:
    return do_work
