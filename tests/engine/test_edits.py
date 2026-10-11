"""Editing a running workflow: each allowed and each refused edit, and forward-only behaviour.

The two example workflows of the design note are walked part-way with the engine, then
edited. Every count in a preview is checked against the state of the walk, worked out by
hand in the walk's docstring.
"""

import pytest

from ltq.engine import (
    EditRefused,
    InvalidRequest,
    InvalidShown,
    ItemCounts,
    StageImpact,
    VersionConflict,
    draw,
    rebuild,
    verify_chain,
)

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW = {"name": "expert_review", "input": "annotate", "assign_to": "expert", "sample": 0.2}
DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}
SEED = 50
ITEMS = [f"img_{i:03d}" for i in range(1, 9)]


def review_config(cfg, *stages, final=("expert_review", "annotate")):
    return cfg(*(stages or (ANNOTATE, REVIEW)), final=list(final))


def double_config(cfg, *stages, final=("adjudicate", "annotate")):
    return cfg(*(stages or (DOUBLE, ADJUDICATE)), final=list(final))


def review_walk(build, cfg, work):
    """ "Annotators label, an expert reviews a 20% sample", walked part-way.

    8 images, seed 50. The expert_review draws: img_001 (0.137) and img_006 (0.006) lie
    below 0.2; img_003 (0.265) and img_007 (0.205) lie between 0.2 and 0.3; the rest lie
    above 0.3.

    Ana and Ben annotate img_001 to img_004. img_001 is admitted to expert_review, where
    its slot is open; img_002 to img_004 are not admitted and are finalized from annotate.
    Ben holds a claim on img_005; img_006 and img_007 wait in annotate; img_008 is
    excluded. So: 8 items, 4 in progress, 3 finished, 1 excluded.
    """
    draws = {item: draw(SEED, "expert_review", item) for item in ITEMS}
    assert {i for i, d in draws.items() if d < 0.2} == {"img_001", "img_006"}
    assert {i for i, d in draws.items() if 0.2 <= d < 0.3} == {"img_003", "img_007"}
    engine = build(review_config(cfg), items=8, seed=SEED)
    for user in ("ana", "ben", "ana", "ben"):
        work(engine, "wf", user)
    assert engine.request_work("wf", "ben").item == "img_005"
    engine.exclude_item("wf", "img_008", "admin", "blurry")
    status = {r["item"]: r["status"] for r in engine.export_items("wf")}
    assert status == {
        "img_001": "in_progress",
        "img_002": "final",
        "img_003": "final",
        "img_004": "final",
        "img_005": "in_progress",
        "img_006": "in_progress",
        "img_007": "in_progress",
        "img_008": "excluded",
    }
    return engine


def double_walk(build, cfg, work, **kwargs):
    """ "2 annotators, adjudicate disagreed", walked part-way (4 images, seed 1234).

    img_001: Ana and Ben agree, so it is finalized from annotate. img_002: they disagree,
    so it waits in adjudicate with one open slot. img_003: Ana labeled it; one annotate slot
    is open. img_004: both annotate slots are open. So: 4 items, 3 in progress, 1 finished.
    """
    engine = build(double_config(cfg), items=4, **kwargs)
    for user, value in (("ana", "cat"), ("ben", "cat"), ("ana", "cat"), ("ben", "dog")):
        work(engine, "wf", user, value)
    work(engine, "wf", "ana", "cat")
    slots = engine.store.select("slots", workflow="wf", status="open")
    assert sorted((s["stage"], s["item"]) for s in slots) == [
        ("adjudicate", "img_002"),
        ("annotate", "img_003"),
        ("annotate", "img_004"),
        ("annotate", "img_004"),
    ]
    return engine


def finish(engine, work, *users):
    """Let the users work, in turn, until none of them is offered anything."""
    while any([work(engine, "wf", user)[0] for user in users]):
        pass


def log_size(engine):
    return len(engine.store.events())


# --- allowed edits, on the review example --------------------------------------------------


def test_raising_the_sample_admits_newly_arriving_items_below_the_new_sample(build, cfg, work):
    """Draws are hashed per (seed, stage, item), so they do not depend on the version: raising
    the sample from 0.2 to 0.3 admits exactly the newly arriving items whose draws lie below
    0.3. Items that arrived before keep their decision, so img_003 (draw 0.265) stays out:
    backfilling it would be a separate, explicit action (deferred)."""
    engine = review_walk(build, cfg, work)
    new = review_config(cfg, ANNOTATE, {**REVIEW, "sample": 0.3})
    preview = engine.preview_edit("wf", new)
    assert preview.ok and (preview.base_version, preview.new_version) == (1, 2)
    [change] = preview.changes
    assert (change.kind, change.stage, change.before, change.after, change.applies) == (
        "sample",
        "expert_review",
        0.2,
        0.3,
        "in_place",
    )
    assert preview.items == ItemCounts(entered=8, in_progress=4, finished=3, excluded=1)
    # img_001 is in the stage (its slot is open). img_005 to img_007 are still in annotate,
    # so they may still arrive: img_006 would be admitted at 0.2; img_006 and img_007 at 0.3.
    # img_002 to img_004 arrived and were not admitted. img_008 is excluded.
    assert preview.stages == (
        StageImpact(
            stage="expert_review",
            status="changed",
            open_slots=1,
            claimed_slots=0,
            in_stage=1,
            may_arrive=3,
            past=3,
            cannot_arrive=1,
            admitted_at_old_sample=1,
            admitted_at_new_sample=2,
        ),
    )
    assert preview.final is None

    assert engine.activate_edit("wf", new, "admin", base_version=1, reason="more review") == []
    finish(engine, work, "ben", "ana")
    rows = engine.store.select("admissions", workflow="wf", stage="expert_review")
    assert {r["item"]: (r["threshold"], r["admitted"], r["config_version"]) for r in rows} == {
        "img_001": (0.2, True, 1),
        "img_002": (0.2, False, 1),
        "img_003": (0.2, False, 1),  # draw 0.265 < 0.3, but it arrived under version 1
        "img_004": (0.2, False, 1),
        "img_005": (0.3, False, 2),
        "img_006": (0.3, True, 2),
        "img_007": (0.3, True, 2),  # draw 0.205: admitted because it arrived under version 2
    }
    for row in rows:
        assert row["draw"] == draw(SEED, "expert_review", row["item"])
    report = [r for r in engine.sampling_report("wf") if r["upstream_labeler"] is None]
    assert [(r["stage"], r["sample"], r["arrived"], r["admitted"]) for r in report] == [
        ("annotate", 1.0, 8, 8),
        ("expert_review", 0.2, 4, 1),
        ("expert_review", 0.3, 3, 2),
    ]


def test_adding_a_stage_reaches_only_items_still_upstream(build, cfg, work):
    engine = review_walk(build, cfg, work)
    second = {"name": "second_read", "input": "annotate", "assign_to": "expert"}
    new = review_config(
        cfg, ANNOTATE, REVIEW, second, final=("expert_review", "second_read", "annotate")
    )
    preview = engine.preview_edit("wf", new)
    assert preview.ok
    assert [(c.kind, c.stage, c.applies) for c in preview.changes] == [
        ("stage_added", "second_read", "in_place"),
        ("final", None, "in_place"),
    ]
    # img_005 to img_007 have not left annotate yet; img_001 already left it, img_002 to
    # img_004 are finished and img_008 is excluded: no backfill for those five.
    assert preview.stages == (
        StageImpact(
            stage="second_read",
            status="added",
            open_slots=0,
            claimed_slots=0,
            in_stage=0,
            may_arrive=3,
            past=0,
            cannot_arrive=5,
        ),
    )
    assert preview.final.model_dump() == {"not_finalized": 4, "finalized": 3, "excluded": 1}

    engine.activate_edit("wf", new, "admin", base_version=1)
    # The new stage is listed last, so its slots come first in the default order.
    work(engine, "wf", "ben")  # img_005 leaves annotate and arrives at second_read
    assert engine.request_work("wf", "dr_smith").stage == "second_read"
    finish(engine, work, "ana", "ben", "dr_smith", "dr_jones")
    rows = engine.store.select("admissions", workflow="wf", stage="second_read")
    assert [(r["item"], r["config_version"]) for r in rows] == [
        ("img_005", 2),
        ("img_006", 2),
        ("img_007", 2),
    ]
    finals = {r["item"]: r for r in engine.store.select("final_labels", workflow="wf")}
    assert finals["img_002"]["config_version"] == 1
    assert finals["img_005"]["source_stage"] == "second_read"
    assert finals["img_001"]["source_stage"] == "expert_review"


def test_removing_a_leaf_stage_after_cancelling_its_slots(build, cfg, work):
    engine = review_walk(build, cfg, work)
    new = cfg(ANNOTATE, final=["annotate"])
    preview = engine.preview_edit("wf", new)
    message = "Stage 'expert_review' still has 1 open slot, so it cannot be removed yet"
    assert not preview.ok and message in preview.errors[0]
    assert "Cancel its open slots first" in preview.errors[0]
    before = log_size(engine)
    with pytest.raises(EditRefused, match=message):
        engine.activate_edit("wf", new, "admin", base_version=1)
    assert log_size(engine) == before

    engine.cancel_open_slots("wf", "expert_review", "admin", "stopping expert review")
    preview = engine.preview_edit("wf", new)
    assert preview.ok
    assert [(c.kind, c.stage, c.applies) for c in preview.changes] == [
        ("stage_removed", "expert_review", "in_place"),
        ("final", None, "in_place"),
    ]
    # Counted under version 1: img_005 to img_007 could still have arrived; img_001 to
    # img_004 arrived earlier (img_001 left with no labels when its slot was cancelled).
    assert preview.stages == (
        StageImpact(
            stage="expert_review",
            status="removed",
            open_slots=0,
            claimed_slots=0,
            in_stage=0,
            may_arrive=3,
            past=4,
            cannot_arrive=1,
        ),
    )
    engine.activate_edit("wf", new, "admin", base_version=1, reason="no expert time")

    finish(engine, work, "ana", "ben", "dr_smith")
    arrivals = engine.store.select("admissions", workflow="wf", stage="expert_review")
    assert sorted(r["item"] for r in arrivals) == ["img_001", "img_002", "img_003", "img_004"]
    assert engine.request_work("wf", "dr_smith") is None
    finals = {r["item"]: r for r in engine.store.select("final_labels", workflow="wf")}
    assert finals["img_006"]["source_stage"] == "annotate"  # draw 0.006: no review any more
    assert finals["img_006"]["config_version"] == 2
    # The removed stage's history stays visible.
    trace = engine.explain("wf", "img_001").trace
    assert [t.stage for t in trace if t.type == "admission_decided"] == [
        "annotate",
        "expert_review",
    ]
    assert any(t.type == "slots_cancelled" and t.stage == "expert_review" for t in trace)
    stages = {r["stage"] for r in engine.sampling_report("wf")}
    assert stages == {"annotate", "expert_review"}


def test_a_stage_with_a_claimed_slot_cannot_be_removed(build, cfg, work):
    engine = review_walk(build, cfg, work)
    engine.request_work("wf", "dr_smith")
    preview = engine.preview_edit("wf", cfg(ANNOTATE, final=["annotate"]))
    assert preview.errors == (
        "Stage 'expert_review' still has 1 claimed slot, so it cannot be removed yet. Cancel "
        "its open slots first (a claimed slot must be submitted or released, and then "
        "cancelled), then preview the edit again.",
    )
    assert preview.changes[0].applies == "refused"


def test_a_removed_and_an_added_stage_alike_look_like_a_rename(build, cfg, work):
    engine = review_walk(build, cfg, work)
    engine.cancel_open_slots("wf", "expert_review", "admin", "renaming")
    renamed = {**REVIEW, "name": "review"}
    new = review_config(cfg, ANNOTATE, renamed, final=("review", "annotate"))
    preview = engine.preview_edit("wf", new)
    assert preview.ok
    assert preview.warnings == (
        "Stage 'expert_review' is removed and stage 'review' is added with the same input "
        "(annotate) and roles (expert). If this is meant as a rename: renaming a stage needs a "
        "new workflow, because labels and decisions are recorded under the stage's name. As "
        "written, 'expert_review' stops receiving items and 'review' starts empty (no "
        "backfill).",
    )
    engine.activate_edit("wf", new, "admin", base_version=1)

    # The old name cannot come back: its admissions and draws are recorded under it.
    final = ("expert_review", "review", "annotate")
    again = review_config(cfg, ANNOTATE, renamed, REVIEW, final=final)
    preview = engine.preview_edit("wf", again)
    assert preview.errors == (
        "Stage 'expert_review' cannot be added: a stage of that name was removed in version 2, "
        "and its admissions, draws and labels are still recorded under that name. Give the new "
        "stage another name.",
    )
    with pytest.raises(EditRefused, match="was removed in version 2"):
        engine.activate_edit("wf", again, "admin", base_version=2)


def test_a_blind_review_edit_does_not_reject_a_label_already_shown_earlier_labels(build, cfg, work):
    engine = review_walk(build, cfg, work)
    slot = engine.request_work("wf", "dr_smith")
    packet = engine.work_packet("wf", slot.slot, "dr_smith")
    assert packet.prior_labels  # version 1 shows Ana's label
    engine.activate_edit(
        "wf", review_config(cfg, ANNOTATE, {**REVIEW, "show_prior": False}), "admin", 1
    )
    label = engine.submit("wf", slot.slot, "dr_smith", {"value": "dog"}, packet.shown())
    row = engine.store.get("labels", label=label)
    assert row["show_prior"] is True and row["shown_prior_ids"] == [1]
    assert row["config_version"] == 2

    # Work shown after the edit is blind, and a label claiming otherwise is refused.
    work(engine, "wf", "ben")  # img_005: not admitted
    work(engine, "wf", "ana")  # img_006: admitted to expert_review
    slot = engine.request_work("wf", "dr_jones")
    blind = engine.work_packet("wf", slot.slot, "dr_jones")
    assert (slot.item, blind.show_prior, blind.prior_labels) == ("img_006", False, ())
    shown = blind.shown().model_copy(update={"show_prior": True, "prior_label_ids": (7,)})
    with pytest.raises(InvalidShown, match="blind"):
        engine.submit("wf", slot.slot, "dr_jones", {"value": "cat"}, shown)


# --- allowed edits, on the adjudication example --------------------------------------------


def test_changing_assign_to_reaches_an_open_slot_at_the_next_claim(build, cfg, work):
    grants = {"ana": ("annotator",), "ben": ("annotator",), "dr_smith": ("expert",)}
    grants |= {"cy": ("senior",)}
    roles = ("annotator", "expert", "senior")
    engine = double_walk(build, cfg, work, grants=grants, roles=roles)
    assert engine.request_work("wf", "cy") is None
    new = double_config(cfg, DOUBLE, {**ADJUDICATE, "assign_to": "senior"})
    preview = engine.preview_edit("wf", new)
    [change] = preview.changes
    assert (change.kind, change.before, change.after) == ("assign_to", ["expert"], ["senior"])
    assert "Applies at the next claim, open slots included" in change.effect
    # img_002 waits in adjudicate; img_003 and img_004 have two annotate slots each, so they
    # may still disagree and arrive; img_001 agreed and is finished.
    assert preview.stages == (
        StageImpact(
            stage="adjudicate",
            status="changed",
            open_slots=1,
            claimed_slots=0,
            in_stage=1,
            may_arrive=2,
            past=0,
            cannot_arrive=1,
        ),
    )
    engine.activate_edit("wf", new, "admin", base_version=1)
    assert engine.request_work("wf", "dr_smith") is None
    slot = engine.request_work("wf", "cy")
    assert (slot.stage, slot.item, slot.config_version) == ("adjudicate", "img_002", 1)
    claimed = engine.store.events()[-1]
    assert (claimed.type, claimed.config_version) == ("slot_claimed", 2)
    assert "adjudicate.assign_to = senior and cy holds senior" in claimed.because


def test_changing_labels_per_item_leaves_existing_slots_as_they_are(build, cfg, work):
    engine = double_walk(build, cfg, work)
    new = double_config(cfg, {**DOUBLE, "labels_per_item": 3}, ADJUDICATE)
    preview = engine.preview_edit("wf", new)
    [change] = preview.changes
    assert (change.kind, change.before, change.after) == ("labels_per_item", 2, 3)
    # annotate takes items from the dataset, so no item already in the workflow arrives again.
    assert preview.stages == (
        StageImpact(
            stage="annotate",
            status="changed",
            open_slots=3,
            claimed_slots=0,
            in_stage=2,
            may_arrive=0,
            past=2,
            cannot_arrive=0,
        ),
    )
    assert any("only 2 people hold" in warning for warning in preview.warnings)
    warnings = engine.activate_edit("wf", new, "admin", base_version=1)
    assert len(warnings) == 1 and "workflow wf, stage 'annotate'" in warnings[0]

    old = engine.store.select("slots", workflow="wf", item="img_004")
    assert [(s["index"], s["config_version"]) for s in old] == [(1, 1), (2, 1)]
    engine.import_item("ds", "img_005", "file:///img_005.png", "sha-005", {}, "admin")
    new_slots = engine.store.select("slots", workflow="wf", item="img_005")
    assert [(s["index"], s["config_version"]) for s in new_slots] == [(1, 2), (2, 2), (3, 2)]
    admission = engine.store.events(item="img_005")[-1]
    assert "annotate.labels_per_item = 3" in admission.because


def test_changing_final_applies_to_items_finalized_later(build, cfg, work):
    engine = double_walk(build, cfg, work)
    new = double_config(cfg, final=("annotate", "adjudicate"))
    preview = engine.preview_edit("wf", new)
    [change] = preview.changes
    assert (change.kind, change.before, change.after) == (
        "final",
        ["adjudicate", "annotate"],
        ["annotate", "adjudicate"],
    )
    assert preview.stages == ()
    assert preview.final.model_dump() == {"not_finalized": 3, "finalized": 1, "excluded": 0}
    engine.activate_edit("wf", new, "admin", base_version=1)

    work(engine, "wf", "dr_smith", "lion")  # adjudicates img_002
    finals = {r["item"]: r for r in engine.store.select("final_labels", workflow="wf")}
    assert (finals["img_001"]["status"], finals["img_001"]["config_version"]) == ("final", 1)
    # Under version 2, annotate comes first, and its labels on img_002 disagreed.
    img_002 = finals["img_002"]
    assert (img_002["status"], img_002["source_stage"], img_002["config_version"]) == (
        "unresolved",
        "annotate",
        2,
    )


def test_adding_a_stage_on_an_agreed_output(build, cfg, work):
    engine = double_walk(build, cfg, work)
    second = {"name": "second_look", "input": "annotate.agreed", "assign_to": "expert"}
    new = double_config(
        cfg, DOUBLE, ADJUDICATE, second, final=("adjudicate", "second_look", "annotate")
    )
    preview = engine.preview_edit("wf", new)
    assert preview.changes[0].effect == (
        "Items that leave 'annotate' after activation arrive at 'second_look' if their labels "
        "agreed; items that already left 'annotate' do not (no backfill)."
    )
    # img_003 and img_004 may still agree; img_001 agreed before the edit (no backfill),
    # and img_002 already left annotate.
    assert preview.stages[0].may_arrive == 2 and preview.stages[0].cannot_arrive == 2
    engine.activate_edit("wf", new, "admin", base_version=1)
    work(engine, "wf", "ben", "cat")  # img_003: Ana and Ben agree
    rows = engine.store.select("admissions", workflow="wf", stage="second_look")
    assert [(r["item"], r["config_version"]) for r in rows] == [("img_003", 2)]


# --- refused edits ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"workflow": "wf2"},
            "The config names workflow 'wf2', but this edit is to workflow 'wf'. A workflow "
            "keeps its name",
        ),
        (
            {"dataset": "other"},
            "Changing the dataset from 'ds' to 'other' needs a new workflow: this workflow's "
            "items and labels belong to 'ds'.",
        ),
        (
            {"label_task": "standin@2"},
            "Changing the label task from 'standin@1' to 'standin@2' needs a new workflow",
        ),
        (
            {"stages": [ANNOTATE, {**REVIEW, "input": None}]},
            "Changing the input of stage 'expert_review' from 'annotate' to every item of the "
            "dataset needs a new workflow",
        ),
        (
            {"final": ["expert_review"]},
            "Stage 'annotate' is not listed in `final`",
        ),
        (
            {"stages": [ANNOTATE, {**REVIEW, "assign_to": "radiologist"}]},
            "The role 'radiologist' does not exist in this project",
        ),
        ({}, "The proposed config is the same as version 1"),
        (
            {"stages": [{**ANNOTATE, "sample": 1, "labels_per_item": 1}, REVIEW]},
            "nothing to activate",
        ),
    ],
)
def test_refused_edits(build, cfg, change, message):
    engine = build(review_config(cfg), items=2, seed=SEED)
    new = review_config(cfg) | change
    preview = engine.preview_edit("wf", new)
    assert not preview.ok
    assert any(message in error for error in preview.errors), preview.errors
    assert all(c.applies == "refused" for c in preview.changes if c.kind != "final")
    before = log_size(engine)
    with pytest.raises(EditRefused, match="The edit was not activated") as refused:
        engine.activate_edit("wf", new, "admin", base_version=1)
    assert refused.value.problems == list(preview.errors)
    assert log_size(engine) == before


def test_existing_stages_cannot_change_order(build, cfg, work):
    triage = {"name": "triage", "assign_to": "expert"}
    engine = build(cfg(ANNOTATE, triage, final=["triage", "annotate"]), items=2)
    preview = engine.preview_edit("wf", cfg(triage, ANNOTATE, final=["triage", "annotate"]))
    [change] = preview.changes
    assert (change.kind, change.before, change.after) == (
        "order",
        ["annotate", "triage"],
        ["triage", "annotate"],
    )
    assert preview.errors == (
        "The existing stages are listed in a different order (annotate, triage becomes "
        "triage, annotate). Their order decides the default work order (later-listed stages "
        "are offered first), so it cannot change in a running workflow. Keep the existing "
        "stages in order; new stages can go anywhere their inputs allow.",
    )
    # A new stage may go between existing ones.
    middle = {"name": "middle", "input": "annotate", "assign_to": "expert"}
    preview = engine.preview_edit(
        "wf", cfg(ANNOTATE, middle, triage, final=["triage", "middle", "annotate"])
    )
    assert preview.ok


def test_an_edit_previewed_against_an_old_version_is_refused(build, cfg, work):
    engine = review_walk(build, cfg, work)
    first = review_config(cfg, ANNOTATE, {**REVIEW, "sample": 0.3})
    second = review_config(cfg, ANNOTATE, {**REVIEW, "show_prior": False})
    assert engine.preview_edit("wf", second).base_version == 1
    engine.activate_edit("wf", first, "alice", base_version=1)
    before = log_size(engine)
    with pytest.raises(VersionConflict) as conflict:
        engine.activate_edit("wf", second, "bob", base_version=1)
    assert str(conflict.value).startswith(
        "Workflow 'wf' is at config version 2 (activated by alice on 2026-10-10 "
    )
    assert str(conflict.value).endswith(
        "but this edit was previewed against version 1. Preview it again against version 2, "
        "then activate it."
    )
    assert log_size(engine) == before
    # Previewed again, the proposal also undoes the first edit, which the preview shows.
    preview = engine.preview_edit("wf", second)
    assert preview.base_version == 2
    assert [c.summary for c in preview.changes] == [
        "expert_review.sample: 0.3 -> 0.2",
        "expert_review.show_prior: true -> false",
    ]
    with pytest.raises(InvalidRequest, match="base_version"):
        engine.activate_edit("wf", second, "bob", base_version="2")
    with pytest.raises(InvalidRequest, match="reason"):
        engine.activate_edit("wf", second, "bob", base_version=2, reason=None)


# --- forward-only, provenance and rebuilding -------------------------------------------------


def test_activation_is_one_logged_event_and_later_decisions_carry_the_new_version(build, cfg, work):
    engine = review_walk(build, cfg, work)
    snapshot = engine.store.snapshot()
    new = review_config(cfg, ANNOTATE, {**REVIEW, "sample": 0.3})
    preview = engine.preview_edit("wf", new)
    engine.activate_edit("wf", new, "admin", base_version=1, reason="more review")

    activated = engine.store.events()[-1]
    assert activated.type == "config_activated"
    assert (activated.config_version, activated.base_version, activated.actor) == (2, 1, "admin")
    assert activated.because == (
        "activated by admin: config version 2 replaces version 1; more review"
    )
    assert activated.config["stages"][1]["sample"] == 0.3
    assert activated.diff == preview.diff and "+    sample: 0.3" in activated.diff
    assert activated.counts == preview.counts()
    assert [c["summary"] for c in activated.changes] == ["expert_review.sample: 0.2 -> 0.3"]
    workflow = engine.store.get("workflows", workflow="wf")
    assert workflow["active_version"] == 2
    versions = engine.store.select("workflow_versions", workflow="wf")
    assert [(v["version"], v["activated_by"]) for v in versions] == [(1, "admin"), (2, "admin")]
    # Only the version tables changed: nothing already decided was touched.
    after = engine.store.snapshot()
    changed = {name for name in after if after[name] != snapshot[name]}
    assert changed == {"workflows", "workflow_versions"}

    finish(engine, work, "ben", "ana", "dr_smith")
    later = [e for e in engine.store.events(workflow="wf") if e.seq > activated.seq]
    assert later and {e.config_version for e in later} == {2}
    labels = {
        r["item"]: r["config_version"] for r in engine.store.select("labels", stage="annotate")
    }
    assert labels == {f"img_00{i}": 1 if i <= 4 else 2 for i in range(1, 8)}
    review = engine.store.select("slots", workflow="wf", stage="expert_review")
    assert [(s["item"], s["config_version"]) for s in review] == [
        ("img_001", 1),
        ("img_006", 2),
        ("img_007", 2),
    ]
    finals = {r["item"]: r["config_version"] for r in engine.store.select("final_labels")}
    assert finals == {f"img_00{i}": 1 if 2 <= i <= 4 else 2 for i in range(1, 8)}

    # explain shows the activation in the trace of an item that was in progress then.
    trace = engine.explain("wf", "img_006").trace
    assert "config_activated" in [t.type for t in trace]
    assert "config_activated" not in [t.type for t in engine.explain("wf", "img_002").trace]
    text = engine.explain("wf", "img_006").render()
    assert "new config   config version 2 of wf is in force" in text


def test_state_rebuilds_from_the_log_after_edits(build, cfg, work):
    engine = review_walk(build, cfg, work)
    review_30 = {**REVIEW, "sample": 0.3}
    second = {"name": "second_read", "input": "annotate", "assign_to": "expert"}
    final = ("second_read", "expert_review", "annotate")
    engine.activate_edit("wf", review_config(cfg, ANNOTATE, review_30), "admin", 1)
    work(engine, "wf", "ben")
    engine.activate_edit("wf", review_config(cfg, ANNOTATE, review_30, second, final=final), "a", 2)
    work(engine, "wf", "ana")
    work(engine, "wf", "dr_smith")
    on_call = {**second, "assign_to": ["expert", "annotator"]}
    engine.activate_edit(
        "wf", review_config(cfg, ANNOTATE, review_30, on_call, final=final), "a", 3
    )
    engine.cancel_open_slots("wf", "expert_review", "admin", "stop reviewing")
    engine.activate_edit(
        "wf", review_config(cfg, ANNOTATE, on_call, final=("second_read", "annotate")), "a", 4
    )
    finish(engine, work, "ana", "ben", "dr_smith", "dr_jones")
    assert engine.store.get("workflows", workflow="wf")["active_version"] == 5
    events = engine.store.events()
    assert verify_chain(events) == len(events)
    assert rebuild(events).snapshot() == engine.store.snapshot()
