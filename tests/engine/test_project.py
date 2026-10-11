"""Project commands (roles, datasets, items) and starting a workflow, with their errors."""

import pytest

from labelflow.engine import (
    AlreadyExists,
    Engine,
    InvalidRequest,
    NewItem,
    NotAllowed,
    UnknownDataset,
    UnknownLabelTaskRef,
    UnknownRole,
    UnknownWorkflow,
)
from labelflow.labeltask import LabelTaskRegistry
from labelflow.labeltask.standin import StandInLabelTask

ONE_STAGE = {"name": "annotate", "assign_to": "annotator"}


@pytest.fixture
def engine(build):
    return build()


def types(engine):
    return [e.type for e in engine.store.events()]


def test_roles_are_logged_events(engine):
    engine.define_role("reviewer", "admin")
    engine.grant_role("cy", "reviewer", "admin", "passed training")
    engine.revoke_role("cy", "reviewer", "admin", "left the project")
    granted, revoked = engine.store.events()[-2:]
    assert (granted.type, granted.user, granted.role, granted.actor) == (
        "role_granted",
        "cy",
        "reviewer",
        "admin",
    )
    assert granted.because == "granted by admin: passed training"
    assert revoked.type == "role_revoked"
    row = engine.store.get("role_assignments", user="cy", role="reviewer")
    assert row["active"] is False
    engine.grant_role("cy", "reviewer", "admin", "back again")
    assert engine.store.get("role_assignments", user="cy", role="reviewer")["active"] is True


@pytest.mark.parametrize(
    ("action", "error", "message"),
    [
        (lambda e: e.define_role("annotator", "admin"), AlreadyExists, "already defined"),
        (lambda e: e.define_role("bad name", "admin"), InvalidRequest, "not a valid role name"),
        (lambda e: e.grant_role("cy", "anotator", "admin", "x"), UnknownRole, "annotator"),
        (lambda e: e.grant_role("ana", "annotator", "admin", "x"), AlreadyExists, "already"),
        (lambda e: e.grant_role("cy", "annotator", "admin", " "), InvalidRequest, "reason"),
        (lambda e: e.revoke_role("cy", "annotator", "admin", "x"), NotAllowed, "does not hold"),
        (lambda e: e.create_dataset("ds", "admin"), AlreadyExists, "already exists"),
        (lambda e: e.import_item("dss", "x", "u", "h", {}, "a"), UnknownDataset, "Did you mean"),
        (lambda e: e.import_item("ds", "img_001", "u", "h", {}, "a"), AlreadyExists, "img_001"),
        (lambda e: e.import_item("ds", "x", "u", "", {}, "a"), InvalidRequest, "content hash"),
        (lambda e: e.import_item("ds", "x", "u", "h", {"k": {1, 2}}, "a"), InvalidRequest, "JSON"),
        (lambda e: e.request_work("nope", "ana"), UnknownWorkflow, "no workflow"),
    ],
)
def test_project_errors(engine, action, error, message):
    before = len(engine.store.events())
    with pytest.raises(error, match=message):
        action(engine)
    assert len(engine.store.events()) == before


def test_start_workflow_records_version_1_and_admits_items_in_import_order(engine, cfg):
    engine.start_workflow(cfg(ONE_STAGE), 99, "admin")
    started = next(e for e in engine.store.events() if e.type == "workflow_started")
    assert (started.seed, started.config_version, started.actor) == (99, 1, "admin")
    assert started.config["stages"][0]["sample"] == 1.0  # the full config, defaults filled in
    version = engine.store.get("workflow_versions", workflow="wf", version=1)
    assert version["config"] == started.config
    entered = [e.item for e in engine.store.events() if e.type == "item_entered"]
    assert entered == ["img_001", "img_002", "img_003", "img_004"]
    slots = engine.store.select("slots", workflow="wf")
    assert [(s["item"], s["status"]) for s in slots] == [
        (f"img_00{i}", "open") for i in range(1, 5)
    ]


def test_importing_into_a_running_workflow_admits_the_item(engine, cfg):
    engine.start_workflow(cfg(ONE_STAGE), 1, "admin")
    engine.start_workflow(cfg(ONE_STAGE, workflow="other"), 2, "admin")
    engine.import_item("ds", "late", "file:///late.png", "sha-late", {"site": "B"}, "admin")
    entered = [e for e in engine.store.events() if e.type == "item_entered" and e.item == "late"]
    assert [e.workflow for e in entered] == ["wf", "other"]
    assert entered[0].because == "wf.dataset = ds; the item was imported while the workflow runs"
    assert engine.store.get("admissions", workflow="other", stage="annotate", item="late")


def new_items(*ids: str) -> list[NewItem]:
    return [NewItem(item=i, uri=f"file:///{i}.png", content_hash=f"sha-{i}") for i in ids]


def test_import_items_adds_a_batch_in_order_into_every_running_workflow(engine, cfg):
    engine.start_workflow(cfg(ONE_STAGE), 1, "admin")
    assert engine.import_items("ds", new_items("b", "a"), "admin") == ["b", "a"]
    imported = [e for e in engine.store.events() if e.type == "item_imported"]
    assert [e.item for e in imported[-2:]] == ["b", "a"]
    assert imported[-1].at == imported[-2].at  # one command, one timestamp
    assert [e.item for e in engine.store.events(type="item_entered")][-2:] == ["b", "a"]
    assert engine.dataset("ds").items == 6
    assert engine.import_items("ds", [], "admin") == []


@pytest.mark.parametrize(
    ("ids", "error", "message"),
    [
        (("x", "img_001"), AlreadyExists, "already has an item 'img_001'"),
        (("x", "y", "x"), InvalidRequest, "'x' is listed twice"),
        (("x", ""), InvalidRequest, "item id is required"),
    ],
)
def test_import_items_is_all_or_nothing(engine, cfg, ids, error, message):
    engine.start_workflow(cfg(ONE_STAGE), 1, "admin")
    events, state = engine.store.events(), engine.store.snapshot()
    with pytest.raises(error, match=message):
        engine.import_items("ds", new_items(*ids), "admin")
    assert engine.store.events() == events and engine.store.snapshot() == state
    with pytest.raises(UnknownDataset):
        engine.import_items("nope", new_items("x"), "admin")


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"dataset": "nope"}, UnknownDataset, "no dataset named 'nope'"),
        ({"label_task": "ontology@1"}, UnknownLabelTaskRef, "ontology@1"),
        (
            {"stages": [{"name": "a", "assign_to": ["annotator", "radiologist"]}]},
            UnknownRole,
            "radiologist",
        ),
    ],
)
def test_start_workflow_errors(engine, cfg, change, error, message):
    before = len(engine.store.events())
    with pytest.raises(error, match=message):
        engine.start_workflow(cfg(ONE_STAGE) | change, 1, "admin")
    assert len(engine.store.events()) == before


def test_a_workflow_name_is_used_once(engine, cfg):
    engine.start_workflow(cfg(ONE_STAGE), 1, "admin")
    with pytest.raises(AlreadyExists, match="'wf' already exists"):
        engine.start_workflow(cfg(ONE_STAGE), 1, "admin")
    with pytest.raises(InvalidRequest, match="seed"):
        engine.start_workflow(cfg(ONE_STAGE, workflow="w2"), "1", "admin")
    for seed in (-1, 2**63):
        with pytest.raises(InvalidRequest, match="seed"):
            engine.start_workflow(cfg(ONE_STAGE, workflow="w2"), seed, "admin")


def test_staffing_warnings(cfg, make_store):
    engine = Engine(make_store(), LabelTaskRegistry([StandInLabelTask()]))
    for role in ("annotator", "expert"):
        engine.define_role(role, "admin")
    engine.grant_role("ana", "annotator", "admin", "setup")
    engine.create_dataset("ds", "admin")
    warnings = engine.start_workflow(
        cfg(
            {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2},
            {"name": "review", "input": "annotate", "assign_to": "expert"},
            final=["review", "annotate"],
        ),
        1,
        "admin",
    )
    assert len(warnings) == 2
    assert "annotate" in warnings[0] and "review" in warnings[1]
    assert len(engine.grant_role("dr_smith", "expert", "admin", "hired")) == 1


def test_a_failing_command_leaves_no_trace(build, cfg):
    class Broken(StandInLabelTask):
        def check_consensus(self, labels):
            raise RuntimeError("label task crashed")

    engine = build(
        cfg({"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}, final=None),
        task=Broken(),
    )
    for user in ("ana", "ben"):
        slot = engine.request_work("wf", user)
        packet = engine.work_packet("wf", slot.slot, user)
        if user == "ana":
            engine.submit("wf", slot.slot, user, {"value": "a"}, packet.shown())
    before_events, before_state = engine.store.events(), engine.store.snapshot()
    with pytest.raises(RuntimeError, match="crashed"):
        engine.submit("wf", slot.slot, "ben", {"value": "a"}, packet.shown())
    assert engine.store.events() == before_events
    assert engine.store.snapshot() == before_state
