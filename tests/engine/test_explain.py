"""explain(item): the trace of what happened, and why a waiting item is waiting."""

import pytest

from labelflow.engine import UnknownItem

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW = {"name": "review", "input": "annotate", "assign_to": "expert"}


def reasons(engine, item, workflow="wf"):
    return [w.reason for w in engine.explain(workflow, item).waiting]


def test_claimed_slot_says_by_whom_and_since_when(build, cfg):
    engine = build(cfg(ANNOTATE), items=1)
    slot = engine.request_work("wf", "ana")
    since = slot.claimed_at.strftime("%Y-%m-%d %H:%M")
    assert reasons(engine, "img_001") == [
        f"slot 1 of annotate (id 1) is claimed by ana since {since} UTC"
    ]


def test_open_slot_names_who_could_claim_it(build, cfg, work):
    engine = build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=1)
    work(engine, "wf", "ana")
    assert reasons(engine, "img_001") == [
        "slot 1 of review (id 2) is open; role expert is held by dr_jones and dr_smith; "
        "dr_jones can claim it and has not asked for work yet; "
        "dr_smith can claim it and has not asked for work yet"
    ]


def test_only_holder_already_labeled_the_item(build, cfg, work):
    grants = {"ana": ("annotator",), "cy": ("annotator", "expert")}
    engine = build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=1, grants=grants)
    work(engine, "wf", "cy")
    assert reasons(engine, "img_001") == [
        "slot 1 of review (id 2) is open; nobody can claim it: cy is the only holder of role "
        "expert, and cy already labeled this item in annotate"
    ]


def test_some_holders_blocked_and_some_busy(build, cfg, work):
    grants = {
        "ana": ("annotator",),
        "dr_smith": ("annotator", "expert"),
        "dr_jones": ("annotator", "expert"),
    }
    engine = build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=2, grants=grants)
    work(engine, "wf", "dr_smith")  # labels img_001 in annotate
    busy = engine.request_work("wf", "dr_jones")  # review of img_001 comes first
    assert (busy.stage, busy.item) == ("review", "img_001")
    work(engine, "wf", "ana")  # labels img_002; its review slot opens
    assert reasons(engine, "img_002") == [
        "slot 1 of review (id 4) is open; role expert is held by dr_jones and dr_smith; "
        f"dr_jones can claim it but is working on slot {busy.slot} (item img_001, review) first; "
        "dr_smith can claim it and has not asked for work yet"
    ]


def test_nobody_holds_the_role(build, cfg):
    engine = build(cfg({"name": "gold_read", "assign_to": "trainee"}), items=1)
    assert reasons(engine, "img_001") == [
        "slot 1 of gold_read (id 1) is open; nobody holds role trainee, so nobody can claim it"
    ]


def test_reserved_slot_whose_person_lost_the_role(build, cfg):
    gold = {"name": "gold_read", "assign_to": "trainee", "labels_per_item": "all"}
    engine = build(cfg(gold, final="none"), items=1, grants={"t1": ("trainee",)})
    engine.revoke_role("t1", "trainee", "admin", "moved on")
    assert reasons(engine, "img_001") == [
        "slot 1 of gold_read (id 1) is open; it is reserved for t1, so nobody can claim it: "
        "t1 does not hold role trainee"
    ]


def test_a_released_item_is_offered_back_only_after_other_work(build, cfg):
    engine = build(cfg(ANNOTATE), items=2, grants={"ana": ("annotator",)})
    slot = engine.request_work("wf", "ana")
    engine.release("wf", slot.slot, "ana", "cannot see the image")
    assert reasons(engine, "img_001") == [
        "slot 1 of annotate (id 1) is open; role annotator is held by ana; ana released it "
        "before; it is offered to ana only after their other work"
    ]
    other = engine.request_work("wf", "ana")
    assert other.item == "img_002"
    assert reasons(engine, "img_001") == [
        "slot 1 of annotate (id 1) is open; role annotator is held by ana; ana released it "
        "before; it is offered to ana only after their other work, and is working on slot "
        f"{other.slot} (item img_002, annotate) now"
    ]


def test_an_open_slot_in_a_paused_stage_says_who_paused_it_when_and_why(build, cfg, work):
    engine = build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=1)
    work(engine, "wf", "ana")
    engine.pause_stage("wf", "review", "admin", "experts are in training")
    (reason,) = reasons(engine, "img_001")
    assert reason.startswith(
        "slot 1 of review (id 2) is open; it waits because stage review is paused (by admin "
        "since 2026-10-10 "
    )
    assert reason.endswith(" UTC: experts are in training)")
    engine.pause_workflow("wf", "admin", "audit")
    (reason,) = reasons(engine, "img_001")
    assert "it waits because workflow wf is paused (by admin since " in reason
    assert ": audit) and stage review is paused (by admin since " in reason
    engine.resume_stage("wf", "review", "admin", "training done")
    (reason,) = reasons(engine, "img_001")
    assert "workflow wf is paused" in reason and "stage review" not in reason
    engine.resume_workflow("wf", "admin", "audit done")
    assert reasons(engine, "img_001")[0].endswith(
        "dr_smith can claim it and has not asked for work yet"
    )


def test_a_held_claim_in_a_paused_stage_is_still_shown_as_claimed(build, cfg):
    engine = build(cfg(ANNOTATE), items=1)
    slot = engine.request_work("wf", "ana")
    engine.pause_stage("wf", "annotate", "admin", "hold on")
    assert reasons(engine, "img_001") == [
        f"slot 1 of annotate (id 1) is claimed by ana since "
        f"{slot.claimed_at.strftime('%Y-%m-%d %H:%M')} UTC"
    ]


def test_a_claim_on_the_item_in_another_stage_blocks_a_second_one(build, cfg):
    first = {"name": "first_read", "assign_to": "annotator"}
    second = {"name": "second_read", "assign_to": "annotator"}
    engine = build(cfg(first, second), items=1, grants={"ana": ("annotator",)})
    slot = engine.request_work("wf", "ana")
    assert slot.stage == "second_read"  # later-listed stages first
    assert reasons(engine, "img_001") == [
        "slot 1 of first_read (id 1) is open; nobody can claim it: ana is the only holder of "
        "role annotator, and ana holds a claim on this item in second_read",
        f"slot 1 of second_read (id 2) is claimed by ana since "
        f"{slot.claimed_at.strftime('%Y-%m-%d %H:%M')} UTC",
    ]


def test_trace_and_render(build, cfg, work):
    engine = build(cfg(ANNOTATE, REVIEW, final=["review", "annotate"]), items=1)
    work(engine, "wf", "ana", "effusion", duration_ms=130_000)
    explanation = engine.explain("wf", "img_001")
    assert explanation.status == "in_progress"
    assert [t.type for t in explanation.trace] == [
        "item_imported",
        "item_entered",
        "admission_decided",
        "slot_claimed",
        "label_submitted",
        "stage_completed",
        "admission_decided",
    ]
    text = explanation.render()
    assert text.startswith("item img_001  in workflow wf  status: in_progress\n")
    assert 'label 1 by ana (annotator) in annotate: {"value":"effusion"} (2m 10s)' in text
    assert "because      review.input = annotate; review.sample = 1 (every item, no draw)" in text
    assert "waiting:\n  - slot 1 of review (id 2) is open;" in text

    work(engine, "wf", "dr_smith", "no effusion")
    explanation = engine.explain("wf", "img_001")
    assert explanation.status == "final" and explanation.waiting == ()
    assert explanation.final.source_stage == "review"
    assert explanation.final.label == {"value": "no effusion"}
    text = explanation.render()
    assert 'final label {"value":"no effusion"}  (config v1)' in text
    assert "final = [review, annotate]: review is the first listed stage that labeled it" in text


def test_explain_excluded_and_unknown_items(build, cfg):
    engine = build(cfg(ANNOTATE), items=1)
    engine.exclude_item("wf", "img_001", "admin", "corrupt file")
    explanation = engine.explain("wf", "img_001")
    assert explanation.status == "excluded" and explanation.waiting == ()
    assert explanation.trace[-1].because == "excluded by admin: corrupt file"
    with pytest.raises(UnknownItem, match="not in workflow"):
        engine.explain("wf", "img_404")


def test_explain_an_item_excluded_after_its_final_label(build, cfg, work):
    engine = build(cfg(ANNOTATE), items=1)
    work(engine, "wf", "ana", "cat")
    engine.exclude_item("wf", "img_001", "admin", "wrong patient")
    explanation = engine.explain("wf", "img_001")
    assert explanation.status == "excluded" and explanation.waiting == ()
    assert explanation.final.status == "final" and explanation.final.label == {"value": "cat"}
    types = [t.type for t in explanation.trace]
    assert types[-2:] == ["item_finalized", "item_excluded"]
    assert explanation.trace[-1].because == (
        "excluded by admin: wrong patient; its earlier final label is kept as history"
    )
    text = explanation.render()
    assert text.startswith("item img_001  in workflow wf  status: excluded\n")
    assert 'before it was excluded: final label {"value":"cat"}  (config v1)' in text
    assert "excluded     from wf (wrong patient); slots cancelled: none" in text
