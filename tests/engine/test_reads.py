"""The read API for the web layer: lists, pages and details as plain objects, on both stores.

Also the read context: a read never writes, a command cannot start inside one, and a read
nested in a command sees the command's own, uncommitted state.
"""

import pytest

from labelflow.engine import (
    ConfigVersion,
    Dataset,
    Item,
    NewItem,
    Role,
    UnknownDataset,
    UnknownRole,
    UnknownWorkflow,
    Workflow,
)
from labelflow.engine import events as ev
from labelflow.engine.config import resolved_yaml

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW = {"name": "review", "input": "annotate", "assign_to": "expert"}


@pytest.fixture
def engine(build, cfg):
    return build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=3)


def test_roles_with_their_holders(engine):
    roles = engine.roles()
    assert [r.role for r in roles] == ["annotator", "expert", "trainee"]
    assert roles[0] == Role(
        role="annotator",
        defined_by="admin",
        defined_at=roles[0].defined_at,
        holders=("ana", "ben"),
    )
    assert roles[0].defined_at.tzinfo is not None
    assert roles[2].holders == ()
    engine.revoke_role("ana", "annotator", "admin", "left")
    assert engine.role("annotator").holders == ("ben",)
    assert engine.user_roles("ben") == {"annotator"} and engine.user_roles("nobody") == set()
    with pytest.raises(UnknownRole, match="Did you mean 'expert'"):
        engine.role("exprt")


def test_datasets_and_their_items(engine):
    engine.create_dataset("other", "admin")
    assert [(d.dataset, d.items) for d in engine.datasets()] == [("ds", 3), ("other", 0)]
    assert engine.dataset("other") == Dataset(
        dataset="other", created_by="admin", created_at=engine.dataset("other").created_at, items=0
    )
    page = engine.items("ds", offset=1, limit=1)
    assert (page.total, page.offset, page.limit) == (3, 1, 1)
    assert page.rows == (
        Item(
            dataset="ds",
            item="img_002",
            uri="file:///data/img_002.png",
            content_hash="sha-002",
            properties={},
            imported_by="admin",
            imported_at=page.rows[0].imported_at,
        ),
    )
    assert [i.item for i in engine.items("ds").rows] == ["img_001", "img_002", "img_003"]
    assert engine.items("ds", offset=5).rows == ()
    with pytest.raises(UnknownDataset, match="Did you mean 'ds'"):
        engine.items("dss")


def test_workflows_with_stages_pauses_and_progress(engine, cfg, work):
    engine.start_workflow(cfg(ANNOTATE, workflow="second"), 5, "admin")
    engine.pause_stage("wf", "review", "admin", "experts away")
    work(engine, "wf", "ana")
    engine.exclude_item("wf", "img_003", "admin", "blurry")

    listed = engine.workflows()
    assert [w.workflow for w in listed] == ["wf", "second"]
    first = listed[0]
    assert isinstance(first, Workflow) and (first.seed, first.active_version) == (1234, 1)
    assert [(s.name, s.roles, s.paused is None) for s in first.stages] == [
        ("annotate", ("annotator",), True),
        ("review", ("expert",), False),
    ]
    assert first.stages[1].paused.reason == "experts away" and first.paused is None

    detail = engine.workflow("wf")
    assert detail.model_dump(exclude={"config", "progress"}) == first.model_dump()
    assert "\nworkflow: wf\n" in resolved_yaml(detail.config)
    assert detail.progress == engine.progress("wf")
    assert detail.progress.items.model_dump() == {
        "entered": 3,
        "in_progress": 2,
        "final": 0,
        "unresolved": 0,
        "none": 0,
        "excluded": 1,
    }
    assert [s.model_dump() for s in detail.progress.stages] == [
        {"stage": "annotate", "open": 1, "claimed": 0, "filled": 1, "cancelled": 1},
        {"stage": "review", "open": 1, "claimed": 0, "filled": 0, "cancelled": 0},
    ]
    with pytest.raises(UnknownWorkflow, match="Did you mean 'wf'"):
        engine.workflow("wfx")


def test_progress_counts_every_item_status_and_removed_stages(engine, cfg, work):
    work(engine, "wf", "ana")
    work(engine, "wf", "dr_smith")  # img_001 final
    engine.exclude_item("wf", "img_002", "admin", "blurry")
    engine.activate_edit("wf", cfg(ANNOTATE, final="none"), "admin", 1, "no review")
    work(engine, "wf", "ben")  # img_003 finalized under version 2: none
    progress = engine.progress("wf")
    assert progress.items.model_dump() == {
        "entered": 3,
        "in_progress": 0,
        "final": 1,
        "unresolved": 0,
        "none": 1,
        "excluded": 1,
    }
    assert [s.stage for s in progress.stages] == ["annotate", "review"]  # removed stage kept


def test_config_versions_carry_the_edit_reason_and_diff(engine, cfg):
    new = cfg(ANNOTATE, {**REVIEW, "sample": 0.5}, final=["review", "annotate"])
    engine.activate_edit("wf", new, "editor", 1, "fewer reviews")
    versions = engine.config_versions("wf")
    assert [(v.version, v.active, v.activated_by) for v in versions] == [
        (1, False, "admin"),
        (2, True, "editor"),
    ]
    assert isinstance(versions[0], ConfigVersion)
    assert (versions[0].reason, versions[0].diff) == (None, None)
    assert versions[1].reason == "fewer reviews" and "+    sample: 0.5" in versions[1].diff
    assert versions[1].config.stage("review").sample == 0.5
    assert versions[0].config_hash != versions[1].config_hash
    assert versions[1].activated_at.tzinfo is not None


def test_the_event_log_is_paged_and_filtered(engine, work):
    work(engine, "wf", "ana")
    everything = engine.store.events()
    page = engine.events(offset=2, limit=3)
    assert (page.total, page.offset, page.limit) == (len(everything), 2, 3)
    assert list(page.rows) == everything[2:5]
    last = engine.events(offset=len(everything) - 1, limit=10)
    assert list(last.rows) == everything[-1:]
    claims = engine.events(type="slot_claimed")
    assert claims.total == 1 and isinstance(claims.rows[0], ev.SlotClaimed)
    about = engine.events(workflow="wf", item="img_002")
    assert about.total == 2 and {e.item for e in about.rows} == {"img_002"}
    assert engine.events(workflow="nope").total == 0
    assert engine.events(limit=0).rows == () and engine.events(limit=0).total == page.total


def test_validation_context_lists_the_projects_roles_datasets_and_tasks(engine):
    context = engine.validation_context()
    assert context.roles == {
        "annotator": ["ana", "ben"],
        "expert": ["dr_jones", "dr_smith"],
        "trainee": [],
    }
    assert context.datasets == ["ds"] and context.label_tasks == ["standin@1"]


def test_reads_change_nothing(engine, work):
    work(engine, "wf", "ana")
    events, state = engine.store.events(), engine.store.snapshot()
    engine.roles()
    engine.datasets()
    engine.items("ds")
    engine.workflows()
    engine.workflow("wf")
    engine.config_versions("wf")
    engine.events()
    engine.explain("wf", "img_001")
    engine.export_items("wf")
    assert engine.store.events() == events and engine.store.snapshot() == state


# --- the read context --------------------------------------------------------------------


def test_a_command_cannot_start_inside_a_read(engine):
    with engine.store.reading(), pytest.raises(RuntimeError, match=r"inside store\.reading"):
        engine.define_role("late", "admin")
    engine.define_role("late", "admin")  # fine once the read is over


def test_writes_are_refused_inside_a_read(engine):
    event = ev.DatasetCreated(at="2026-10-10T09:00:00Z", dataset="x", because="test")
    with engine.store.reading():
        with pytest.raises(RuntimeError, match=r"append\(\) cannot be called inside"):
            engine.store.append(event)
        with pytest.raises(RuntimeError, match=r"insert\(\) cannot be called inside"):
            engine.store.insert("counters", {"name": "x", "value": 1})
    assert engine.store.get("counters", name="x") is None


def test_a_read_inside_a_command_sees_the_commands_own_changes(engine):
    with engine.store.transaction():
        engine.import_items("ds", [NewItem(item="new", uri="u", content_hash="h")], "admin")
        with engine.store.reading():
            assert engine.dataset("ds").items == 4
            assert engine.events(type="item_imported").total == 4
        engine.create_dataset("inner", "admin")  # a command may still follow a nested read
    assert [d.dataset for d in engine.datasets()] == ["ds", "inner"]


def test_reads_nest(engine):
    with engine.store.reading(), engine.store.reading():
        assert engine.dataset("ds").items == 3
    assert engine.dataset("ds").items == 3
