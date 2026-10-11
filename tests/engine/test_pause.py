"""Pausing and resuming a stage or a whole workflow: a pause stops claims, and only claims."""

import pytest

from labelflow.engine import (
    InvalidRequest,
    NotAllowed,
    Paused,
    UnknownStage,
    UnknownWorkflow,
    rebuild,
)

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW = {"name": "review", "input": "annotate", "assign_to": "expert"}


@pytest.fixture
def engine(build, cfg):
    return build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=2)


def test_a_paused_stage_hands_out_no_work_and_refuses_claims(engine):
    engine.pause_stage("wf", "annotate", "admin", "new guidelines coming")
    event = engine.store.events()[-1]
    assert (event.type, event.stage, event.actor, event.reason) == (
        "stage_paused",
        "annotate",
        "admin",
        "new guidelines coming",
    )
    assert event.because == (
        "paused by admin: new guidelines coming; no slot of the stage annotate of wf can be claimed"
    )
    assert engine.request_work("wf", "ana") is None
    before = len(engine.store.events())
    with pytest.raises(Paused, match="stage annotate is paused \\(by admin since 2026-") as error:
        engine.claim("wf", 1, "ana")
    assert str(error.value).endswith(
        "new guidelines coming). It can be claimed once that is resumed."
    )
    assert len(engine.store.events()) == before
    engine.resume_stage("wf", "annotate", "admin", "guidelines are out")
    assert engine.store.events()[-1].type == "stage_resumed"
    assert engine.request_work("wf", "ana").stage == "annotate"


def test_other_stages_keep_working_while_one_is_paused(engine, work):
    work(engine, "wf", "ana")  # img_001's review slot opens
    engine.pause_stage("wf", "annotate", "admin", "hold")
    assert engine.request_work("wf", "ben") is None
    slot, _ = work(engine, "wf", "dr_smith")
    assert (slot.stage, slot.item) == ("review", "img_001")
    assert engine.export_items("wf")[0]["status"] == "final"


def test_a_paused_workflow_hands_out_nothing(engine, work):
    work(engine, "wf", "ana")
    engine.pause_workflow("wf", "admin", "data audit")
    event = engine.store.events()[-1]
    assert (event.type, event.stage, event.reason) == ("workflow_paused", None, "data audit")
    for user in ("ana", "ben", "dr_smith"):
        assert engine.request_work("wf", user) is None
    with pytest.raises(Paused, match="workflow wf is paused"):
        engine.claim("wf", 3, "dr_smith")
    engine.resume_workflow("wf", "admin", "audit done")
    assert engine.request_work("wf", "dr_smith").stage == "review"


def test_held_claims_can_be_submitted_or_released_while_paused(engine):
    held = engine.request_work("wf", "ana")
    other = engine.request_work("wf", "ben")
    engine.pause_workflow("wf", "admin", "audit")
    engine.pause_stage("wf", "annotate", "admin", "guidelines")
    assert engine.request_work("wf", "ana").slot == held.slot  # the held claim comes back
    packet = engine.work_packet("wf", held.slot, "ana")
    engine.submit("wf", held.slot, "ana", {"value": "cat"}, packet.shown())
    engine.release("wf", other.slot, "ben", "end of shift")
    assert engine.slot("wf", other.slot).status == "open"
    # The submit moved img_001 on: it left annotate and was admitted to review, whose slot
    # opens and waits.
    review = engine.store.select("slots", workflow="wf", stage="review", item=held.item)
    assert [s["status"] for s in review] == ["open"]
    assert engine.request_work("wf", "dr_smith") is None


def test_consensus_and_finalization_continue_while_paused(build, cfg):
    double = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
    engine = build(cfg(double), items=1)
    first = engine.request_work("wf", "ana")
    second = engine.request_work("wf", "ben")
    engine.pause_workflow("wf", "admin", "audit")
    for user, slot in (("ana", first), ("ben", second)):
        packet = engine.work_packet("wf", slot.slot, user)
        engine.submit("wf", slot.slot, user, {"value": "cat"}, packet.shown())
    types = [e.type for e in engine.store.events(workflow="wf", item="img_001")]
    assert types[-3:] == ["consensus_checked", "stage_completed", "item_finalized"]
    assert engine.export_items("wf")[0]["status"] == "final"


def test_items_keep_arriving_while_paused(engine):
    engine.pause_workflow("wf", "admin", "audit")
    engine.pause_stage("wf", "annotate", "admin", "guidelines")
    engine.import_item("ds", "img_new", "file:///new.png", "sha-new", {}, "admin")
    (slot,) = engine.store.select("slots", workflow="wf", item="img_new")
    assert slot["status"] == "open"
    assert engine.request_work("wf", "ana") is None


def test_workflow_and_stage_pauses_are_independent(engine, work):
    engine.pause_stage("wf", "annotate", "admin", "guidelines")
    engine.pause_workflow("wf", "admin", "audit")
    engine.resume_workflow("wf", "admin", "audit done")
    resumed = engine.store.events()[-1]
    assert resumed.because == (
        "resumed by admin: audit done; slots can be claimed again, except in paused stage(s) "
        "annotate"
    )
    assert engine.request_work("wf", "ana") is None  # annotate is still paused
    state = engine.pauses("wf")
    assert state.paused is None and state.stages["annotate"].reason == "guidelines"
    engine.pause_workflow("wf", "admin", "audit again")
    engine.resume_stage("wf", "annotate", "admin", "guidelines out")
    assert engine.store.events()[-1].because == (
        "resumed by admin: guidelines out; its slots can be claimed once workflow wf is resumed too"
    )
    assert engine.request_work("wf", "ana") is None  # the workflow is still paused
    engine.resume_workflow("wf", "admin", "done")
    assert engine.store.events()[-1].because == (
        "resumed by admin: done; slots of the workflow wf can be claimed again"
    )
    assert engine.request_work("wf", "ana") is not None


def test_pause_state_read(engine):
    state = engine.pauses("wf")
    assert (state.workflow, state.paused, state.stages) == (
        "wf",
        None,
        {"annotate": None, "review": None},
    )
    engine.pause_stage("wf", "review", "admin", "experts away")
    engine.pause_workflow("wf", "boss", "audit")
    state = engine.pauses("wf")
    assert (state.paused.by, state.paused.reason) == ("boss", "audit")
    assert state.paused.since.tzinfo is not None
    assert state.stages["annotate"] is None
    assert (state.stages["review"].by, state.stages["review"].reason) == ("admin", "experts away")


def test_refused_pauses_and_resumes(engine):
    engine.pause_stage("wf", "annotate", "admin", "hold")
    engine.pause_workflow("wf", "admin", "audit")
    before = len(engine.store.events())
    with pytest.raises(NotAllowed, match=r"^The stage annotate of wf is already paused \(by admin"):
        engine.pause_stage("wf", "annotate", "admin", "again")
    with pytest.raises(NotAllowed, match=r"^The workflow wf is already paused \(by admin since"):
        engine.pause_workflow("wf", "admin", "again")
    with pytest.raises(NotAllowed, match="stage review of wf is not paused, so there is nothing"):
        engine.resume_stage("wf", "review", "admin", "nothing to resume")
    with pytest.raises(UnknownStage, match="Did you mean 'annotate'"):
        engine.pause_stage("wf", "anotate", "admin", "typo")
    with pytest.raises(UnknownWorkflow):
        engine.pause_workflow("nope", "admin", "typo")
    with pytest.raises(InvalidRequest, match="reason"):
        engine.pause_stage("wf", "review", "admin", " ")
    assert len(engine.store.events()) == before
    engine.resume_workflow("wf", "admin", "done")
    with pytest.raises(NotAllowed, match=r"^The workflow wf is not paused"):
        engine.resume_workflow("wf", "admin", "again")


def test_a_paused_stage_stays_paused_across_config_versions(engine):
    engine.pause_stage("wf", "review", "admin", "experts away")
    engine.pause_workflow("wf", "admin", "audit")
    new = engine.store.get("workflow_versions", workflow="wf", version=1)["config"]
    new["stages"][1]["sample"] = 0.5
    preview = engine.preview_edit("wf", new)
    assert preview.ok
    pause_notes = [w for w in preview.warnings if "paused" in w]
    assert len(pause_notes) == 2
    assert pause_notes[0].startswith("The workflow is paused (by admin since 2026-")
    assert pause_notes[0].endswith(": audit); it stays paused under version 2.")
    assert pause_notes[1].startswith("Stage 'review' is paused (by admin since ")
    assert pause_notes[1].endswith(": experts away); it stays paused under version 2.")
    assert "it stays paused under version 2" in " ".join(preview.render().split())
    engine.activate_edit("wf", new, "admin", 1, "sample less")
    state = engine.pauses("wf")
    assert state.paused is not None and state.stages["review"] is not None


def test_a_paused_stage_without_live_slots_can_be_removed(engine, cfg):
    engine.pause_stage("wf", "review", "admin", "not needed")
    smaller = cfg(ANNOTATE, final=["annotate"])
    preview = engine.preview_edit("wf", smaller)
    assert preview.ok
    assert "Stage 'review' is paused; removing it ends the pause with it." in preview.warnings
    engine.activate_edit("wf", smaller, "admin", 1, "drop review")
    assert engine.pauses("wf").stages == {"annotate": None}
    with pytest.raises(UnknownStage):
        engine.resume_stage("wf", "review", "admin", "it is gone")


def test_a_paused_stage_with_open_slots_cannot_be_removed(engine, cfg, work):
    work(engine, "wf", "ana")  # a review slot opens
    engine.pause_stage("wf", "review", "admin", "hold")
    preview = engine.preview_edit("wf", cfg(ANNOTATE, final=["annotate"]))
    assert not preview.ok
    assert any("open" in error for error in preview.errors)


def test_pauses_rebuild_from_the_log(engine, work):
    work(engine, "wf", "ana")
    engine.pause_stage("wf", "review", "admin", "hold")
    engine.pause_workflow("wf", "admin", "audit")
    engine.resume_stage("wf", "review", "admin", "go")
    engine.pause_stage("wf", "annotate", "admin", "hold annotate")
    snapshot = engine.store.snapshot()
    assert rebuild(engine.store.events()).snapshot() == snapshot
    assert [r["paused"] for r in snapshot["stage_pauses"]] == [True, False]
    assert [r["paused"] for r in snapshot["workflow_pauses"]] == [True]
