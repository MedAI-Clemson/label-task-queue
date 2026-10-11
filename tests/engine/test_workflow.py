"""Workflow behaviour: admission, slots, claims, submit, consensus, scoring, final labels."""

import pytest

from ltq.engine import (
    InvalidLabel,
    InvalidRequest,
    InvalidShown,
    NotAllowed,
    NotEligible,
    SlotNotYours,
    UnknownItem,
    UnknownSlot,
    UnknownStage,
    draw,
    rebuild,
)

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW_20 = {"name": "expert_review", "input": "annotate", "assign_to": "expert", "sample": 0.2}
DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}


def events_of(engine, item, workflow="wf"):
    return [e for e in engine.store.events(workflow=workflow, item=item)]


def final_row(engine, item, workflow="wf"):
    return engine.store.get("final_labels", workflow=workflow, item=item)


# --- the design note's walkthrough -------------------------------------------------------


def test_one_images_path(build, cfg):
    """Image 17: annotated by Ana, sampled into expert review, labeled, finalized."""
    seed = next(s for s in range(1000) if draw(s, "expert_review", "img_017") < 0.2)
    engine = build(items=0)
    engine.import_item("ds", "img_017", "file:///data/img_017.png", "sha-17", {}, "admin")
    engine.start_workflow(cfg(ANNOTATE, REVIEW_20, final=["expert_review", "annotate"]), seed, "a")

    # 1. Image 17 is admitted to annotate, which needs one label, so one slot opens.
    admission = events_of(engine, "img_017")[-1]
    assert (admission.type, admission.stage, admission.admitted) == (
        "admission_decided",
        "annotate",
        True,
    )
    assert admission.draw is None and len(admission.slots) == 1
    assert "annotate.sample = 1" in admission.because

    # 2. Ana asks for work, claims the slot, and labels the image.
    slot = engine.request_work("wf", "ana")
    assert (slot.item, slot.stage, slot.claimed_by) == ("img_017", "annotate", "ana")
    packet = engine.work_packet("wf", slot.slot, "ana")
    ana_label = engine.submit(
        "wf", slot.slot, "ana", {"value": "effusion"}, packet.shown(), 130_000
    )

    # 3. On leaving annotate, expert_review samples 20%; image 17's draw is below 0.2.
    left, review = events_of(engine, "img_017")[-2:]
    assert (left.type, left.outputs) == ("stage_completed", ("annotate",))
    assert (review.type, review.stage, review.admitted) == (
        "admission_decided",
        "expert_review",
        True,
    )
    assert review.draw == draw(seed, "expert_review", "img_017") < 0.2 == review.threshold
    assert review.because.startswith(
        "expert_review.input = annotate; expert_review.sample = 0.2 (draw"
    )
    assert final_row(engine, "img_017") is None

    # 4. The expert claims and labels it. Ana's label is shown to the reviewer.
    slot = engine.request_work("wf", "dr_smith")
    packet = engine.work_packet("wf", slot.slot, "dr_smith")
    assert [p.label for p in packet.prior_labels] == [ana_label]
    expert_label = engine.submit(
        "wf", slot.slot, "dr_smith", {"value": "no effusion"}, packet.shown(), 60_000
    )

    # 5. No later stage takes it, so the final step creates its final label.
    final = events_of(engine, "img_017")[-1]
    assert final.type == "item_finalized"
    assert (final.status, final.label, final.source_stage, final.label_ids) == (
        "final",
        {"value": "no effusion"},
        "expert_review",
        (expert_label,),
    )
    assert final.because.startswith("final = [expert_review, annotate]: expert_review is the first")
    assert [t.type for t in engine.explain("wf", "img_017").trace] == [
        "item_imported",
        "item_entered",
        "admission_decided",
        "slot_claimed",
        "label_submitted",
        "stage_completed",
        "admission_decided",
        "slot_claimed",
        "label_submitted",
        "stage_completed",
        "item_finalized",
    ]
    assert all(e.because for e in engine.store.events())


# --- admission ---------------------------------------------------------------------------


def test_sampling_logs_every_decision_with_its_draw(build, cfg, work):
    engine = build(cfg(ANNOTATE, REVIEW_20, final=["expert_review", "annotate"]), items=30)
    while work(engine, "wf", "ana")[0]:
        pass
    decisions = engine.store.select("admissions", workflow="wf", stage="expert_review")
    assert len(decisions) == 30
    for row in decisions:
        assert row["draw"] == draw(1234, "expert_review", row["item"])
        assert row["admitted"] == (row["draw"] < 0.2)
        assert row["threshold"] == 0.2 and row["source"] == "annotate"
        assert row["n_slots"] == (1 if row["admitted"] else 0)
    not_admitted = next(
        e for e in engine.store.events() if e.type == "admission_decided" and not e.admitted
    )
    assert ">= 0.2, not admitted" in not_admitted.because
    assert not not_admitted.slots


def test_labels_per_item_number_opens_that_many_slots(build, cfg):
    engine = build(cfg({**ANNOTATE, "labels_per_item": 3}), items=2)
    slots = engine.store.select("slots", workflow="wf", item="img_001")
    assert [s["index"] for s in slots] == [1, 2, 3]


def test_labels_per_item_fraction_uses_its_own_draw(build, cfg):
    setting = {"count": 3, "fraction": 0.5}
    engine = build(cfg({**ANNOTATE, "labels_per_item": setting}), items=12)
    counts = set()
    for row in engine.store.select("admissions", workflow="wf"):
        value = draw(1234, "annotate", row["item"], "labels_per_item")
        assert row["labels_draw"] == value
        assert row["n_slots"] == (3 if value < 0.5 else 1)
        counts.add(row["n_slots"])
    assert counts == {1, 3}


def test_labels_per_item_all_reserves_a_slot_for_each_holder_at_admission(build, cfg, work):
    grants = {"t1": ("trainee",), "t2": ("trainee",), "t3": ("trainee",)}
    gold = {"name": "gold_read", "assign_to": "trainee", "labels_per_item": "all"}
    engine = build(cfg(gold, final="none"), items=2, grants=grants)
    slots = engine.store.select("slots", workflow="wf", item="img_001")
    assert [s["reserved_for"] for s in slots] == ["t1", "t2", "t3"]
    admission = next(e for e in engine.store.events() if e.type == "admission_decided")
    assert "one each for t1, t2 and t3" in admission.because

    # A later promotion adds no work on old items, only on items admitted after it.
    engine.grant_role("t4", "trainee", "admin", "joined")
    assert engine.request_work("wf", "t4") is None
    engine.import_item("ds", "img_new", "file:///new.png", "sha-new", {}, "admin")
    assert engine.request_work("wf", "t4").item == "img_new"

    # A reserved slot is only for its person.
    slot = engine.request_work("wf", "t1")
    assert slot.reserved_for == "t1"


def test_labels_per_item_all_leaves_out_people_who_labeled_the_item(build, cfg, work):
    review_all = {
        "name": "review",
        "input": "annotate",
        "assign_to": ["annotator", "expert"],
        "labels_per_item": "all",
    }
    engine = build(cfg(ANNOTATE, review_all, final=["review", "annotate"]), items=1)
    work(engine, "wf", "ana")
    slots = engine.store.select("slots", workflow="wf", stage="review")
    assert [s["reserved_for"] for s in slots] == ["ben", "dr_jones", "dr_smith"]


def test_labels_per_item_all_with_nobody_completes_the_stage_at_once(build, cfg):
    gold = {"name": "gold_read", "assign_to": "trainee", "labels_per_item": "all"}
    engine = build(cfg(gold), items=1)
    types = [e.type for e in events_of(engine, "img_001")]
    assert types[-3:] == ["admission_decided", "stage_completed", "item_finalized"]
    assert final_row(engine, "img_001")["status"] == "none"
    assert (
        engine.store.get("admissions", workflow="wf", stage="gold_read", item="img_001")["outputs"]
        == []
    )


# --- claiming ----------------------------------------------------------------------------


def test_request_work_returns_the_current_claim(build, cfg):
    engine = build(cfg(ANNOTATE))
    first = engine.request_work("wf", "ana")
    assert engine.request_work("wf", "ana") == first
    assert len([e for e in engine.store.events() if e.type == "slot_claimed"]) == 1


def test_default_order_later_stages_first_then_oldest_item(build, cfg, work):
    review = {"name": "review", "input": "annotate", "assign_to": "annotator"}
    engine = build(cfg(ANNOTATE, review, final=["review", "annotate"]))
    assert work(engine, "wf", "ana")[0].item == "img_001"
    assert work(engine, "wf", "ana")[0].item == "img_002"
    slot = engine.request_work("wf", "ben")
    assert (slot.stage, slot.item) == ("review", "img_001")
    # Ana may not review her own items, so she continues annotating.
    assert (engine.request_work("wf", "ana").stage) == "annotate"


def test_nobody_labels_the_same_item_twice(build, cfg, work):
    engine = build(cfg(DOUBLE), items=2)
    assert work(engine, "wf", "ana")[0].item == "img_001"
    assert work(engine, "wf", "ana")[0].item == "img_002"
    assert engine.request_work("wf", "ana") is None
    assert work(engine, "wf", "ben")[0].item == "img_001"


def test_no_self_review_across_stages(build, cfg, work):
    grants = {"cy": ("annotator", "expert"), "dr_smith": ("expert",)}
    review = {"name": "review", "input": "annotate", "assign_to": "expert"}
    engine = build(cfg(ANNOTATE, review, final=["review", "annotate"]), items=1, grants=grants)
    work(engine, "wf", "cy")
    assert engine.request_work("wf", "cy") is None
    assert engine.request_work("wf", "dr_smith").stage == "review"


def test_roles_are_checked_at_the_moment_of_the_claim(build, cfg):
    engine = build(cfg(ANNOTATE))
    engine.revoke_role("ana", "annotator", "admin", "on leave")
    assert engine.request_work("wf", "ana") is None
    assert engine.request_work("wf", "dr_smith") is None
    engine.grant_role("dr_smith", "annotator", "admin", "helping out")
    assert engine.request_work("wf", "dr_smith").stage == "annotate"


def test_claim_a_particular_slot(build, cfg, work):
    engine = build(cfg(DOUBLE), items=2)
    slot = engine.claim("wf", 3, "ana")
    assert (slot.item, slot.index, slot.claimed_by) == ("img_002", 1, "ana")
    with pytest.raises(NotAllowed, match="already holds slot 3"):
        engine.claim("wf", 1, "ana")
    with pytest.raises(NotAllowed, match="not open: it is claimed"):
        engine.claim("wf", 3, "ben")
    with pytest.raises(NotEligible, match="does not hold role annotator"):
        engine.claim("wf", 1, "dr_smith")
    with pytest.raises(UnknownSlot):
        engine.claim("wf", 99, "ben")
    engine.submit("wf", 3, "ana", {"value": "a"}, engine.work_packet("wf", 3, "ana").shown())
    with pytest.raises(NotEligible, match="ana already labeled this item in annotate"):
        engine.claim("wf", 4, "ana")


# --- the work packet ---------------------------------------------------------------------


def test_work_packet_shows_prior_labels_and_consensus_but_no_names_or_properties(build, cfg, work):
    engine = build(
        cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]),
        items=1,
        properties=lambda i: {"gold_key": {"value": "secret"}},
    )
    _, first = work(engine, "wf", "ana", "cat")
    _, second = work(engine, "wf", "ben", "dog")
    slot = engine.request_work("wf", "dr_smith")
    packet = engine.work_packet("wf", slot.slot, "dr_smith")
    assert [(p.label, p.stage, p.value) for p in packet.prior_labels] == [
        (first, "annotate", {"value": "cat"}),
        (second, "annotate", {"value": "dog"}),
    ]
    assert packet.consensus.agreed is False
    assert packet.consensus.detail == {"values": ["cat", "dog"]}
    assert packet.consensus.rule_version == "equal-values@1"
    dumped = packet.model_dump_json()
    assert "secret" not in dumped and "ana" not in dumped and "ben" not in dumped
    assert packet.shown().prior_label_ids == (first, second)


def test_blind_stage_shows_nothing_earlier(build, cfg, work):
    blind = {**ADJUDICATE, "show_prior": False}
    engine = build(cfg(DOUBLE, blind, final=["adjudicate", "annotate"]), items=1)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    slot = engine.request_work("wf", "dr_smith")
    packet = engine.work_packet("wf", slot.slot, "dr_smith")
    assert packet.prior_labels == () and packet.consensus is None and not packet.show_prior
    shown = packet.shown().model_copy(update={"prior_label_ids": (1,), "show_prior": True})
    with pytest.raises(InvalidShown, match="blind"):
        engine.submit("wf", slot.slot, "dr_smith", {"value": "cat"}, shown)


def test_work_packet_is_only_for_the_claimant(build, cfg):
    engine = build(cfg(ANNOTATE))
    slot = engine.request_work("wf", "ana")
    with pytest.raises(SlotNotYours, match="claimed by ana"):
        engine.work_packet("wf", slot.slot, "ben")
    with pytest.raises(SlotNotYours, match="it is open"):
        engine.work_packet("wf", 2, "ben")
    with pytest.raises(UnknownSlot):
        engine.work_packet("wf", 42, "ben")


# --- submitting --------------------------------------------------------------------------


def test_submit_records_what_was_shown_and_who_submitted(build, cfg):
    engine = build(cfg(ANNOTATE))
    slot = engine.request_work("wf", "ana")
    packet = engine.work_packet("wf", slot.slot, "ana")
    label_id = engine.submit("wf", slot.slot, "ana", {"value": "cat"}, packet.shown(), 4200)
    event = engine.store.events()[-3]
    assert event.type == "label_submitted"
    assert (event.label_id, event.user, event.roles, event.label) == (
        label_id,
        "ana",
        ("annotator",),
        {"value": "cat"},
    )
    assert event.shown.content_hash == "sha-001" and event.shown.show_prior is True
    assert (event.ontology_version, event.label_task, event.duration_ms) == (
        "standin-ontology@1",
        "standin@1",
        4200,
    )
    assert engine.slot("wf", slot.slot).status == "filled"


@pytest.mark.parametrize(
    ("label", "problem"),
    [
        ({"value": ""}, "non-empty string"),
        ({"value": "cat", "colour": "red"}, "Unknown field 'colour'"),
        (["cat"], "JSON object"),
    ],
)
def test_an_invalid_label_is_rejected_without_an_event(build, cfg, label, problem):
    engine = build(cfg(ANNOTATE))
    slot = engine.request_work("wf", "ana")
    shown = engine.work_packet("wf", slot.slot, "ana").shown()
    before = len(engine.store.events())
    with pytest.raises(InvalidLabel, match=problem) as caught:
        engine.submit("wf", slot.slot, "ana", label, shown)
    assert caught.value.problems
    assert len(engine.store.events()) == before


@pytest.mark.parametrize(
    ("shown", "message"),
    [
        ({"content_hash": "sha-999", "show_prior": True}, "has content hash sha-001"),
        ({"content_hash": "sha-001", "show_prior": True, "prior_label_ids": [2]}, "not labels"),
        ({"content_hash": "sha-001", "show_prior": False, "prior_label_ids": [1]}, "show_prior"),
        (
            {"content_hash": "sha-001", "show_prior": True, "consensus_rule_version": "x@1"},
            "No consensus check",
        ),
        ({"content_hash": "sha-001"}, "malformed"),
    ],
)
def test_shown_references_are_checked(build, cfg, work, shown, message):
    review = {"name": "review", "input": "annotate", "assign_to": "expert"}
    engine = build(cfg(ANNOTATE, review, final=["review", "annotate"]), items=2)
    work(engine, "wf", "ana")  # label 1, on img_001
    work(engine, "wf", "ana")  # label 2, on img_002
    slot = engine.request_work("wf", "dr_smith")
    assert slot.item == "img_001"
    with pytest.raises(InvalidShown, match=message):
        engine.submit("wf", slot.slot, "dr_smith", {"value": "cat"}, shown)


def test_submit_errors(build, cfg):
    engine = build(cfg(ANNOTATE))
    slot = engine.request_work("wf", "ana")
    shown = engine.work_packet("wf", slot.slot, "ana").shown()
    with pytest.raises(SlotNotYours):
        engine.submit("wf", slot.slot, "ben", {"value": "cat"}, shown)
    with pytest.raises(InvalidRequest, match="duration_ms"):
        engine.submit("wf", slot.slot, "ana", {"value": "cat"}, shown, duration_ms=-5)
    engine.submit("wf", slot.slot, "ana", {"value": "cat"}, shown)
    with pytest.raises(SlotNotYours, match="already has its label"):
        engine.submit("wf", slot.slot, "ana", {"value": "cat"}, shown)


# --- consensus and the final step --------------------------------------------------------


def test_agreed_labels_finalize_from_the_agreed_label(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=1)
    _, first = work(engine, "wf", "ana", "cat")
    _, second = work(engine, "wf", "ben", "cat")
    consensus, left, final = events_of(engine, "img_001")[-3:]
    assert (consensus.type, consensus.agreed, consensus.label_ids) == (
        "consensus_checked",
        True,
        (first, second),
    )
    assert consensus.rule_version == "equal-values@1" and consensus.detail
    assert left.outputs == ("annotate", "annotate.agreed")
    assert (final.status, final.label, final.source_stage, final.label_ids) == (
        "final",
        {"value": "cat"},
        "annotate",
        (first, second),
    )
    assert "agreed" in final.rule


def test_disagreed_labels_go_to_adjudication(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=1)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    left, admission = events_of(engine, "img_001")[-2:]
    assert left.outputs == ("annotate", "annotate.disagreed")
    assert admission.stage == "adjudicate" and admission.source == "annotate.disagreed"
    assert admission.because.startswith("adjudicate.input = annotate.disagreed")
    _, expert = work(engine, "wf", "dr_smith", "lion")
    assert final_row(engine, "img_001")["value"] == {"value": "lion"}
    assert final_row(engine, "img_001")["label_ids"] == [expert]


def test_disagreement_in_the_deciding_stage_is_unresolved(build, cfg, work):
    engine = build(cfg(DOUBLE), items=1)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    row = final_row(engine, "img_001")
    assert (row["status"], row["value"], row["source_stage"]) == ("unresolved", None, "annotate")


def test_final_none_creates_no_final_labels(build, cfg, work):
    engine = build(cfg(ANNOTATE, final="none"), items=1)
    work(engine, "wf", "ana")
    row = final_row(engine, "img_001")
    assert (row["status"], row["value"], row["rule"]) == ("none", None, "final: none")
    assert engine.export_items("wf")[0]["status"] == "none"


def test_an_item_no_root_stage_admits_is_done_with_no_final_label(build, cfg):
    seed = next(s for s in range(1000) if draw(s, "annotate", "img_001") >= 0.5)
    engine = build(cfg({**ANNOTATE, "sample": 0.5}), items=1, seed=seed)
    row = final_row(engine, "img_001")
    assert (row["status"], row["rule"]) == ("none", "no stage listed in final labeled it")


# --- scoring -----------------------------------------------------------------------------


def test_labels_are_scored_against_the_key_without_changing_routing(build, cfg, work):
    keys = {1: {"gold_key": {"value": "cat"}}, 2: {"gold_key": {"value": "dog"}}, 3: {}}
    keys[4] = {"gold_key": "not a label"}
    scored = {**ANNOTATE, "score_against": "gold_key"}
    engine = build(cfg(scored), items=4, properties=keys.__getitem__)
    for _ in range(4):
        work(engine, "wf", "ana", "cat")
    scores = {r["item"]: r for r in engine.store.select("scores", workflow="wf")}
    assert (scores["img_001"]["key_status"], scores["img_001"]["agreed"]) == ("scored", True)
    assert (scores["img_002"]["key_status"], scores["img_002"]["agreed"]) == ("scored", False)
    assert (scores["img_003"]["key_status"], scores["img_003"]["agreed"]) == ("no_key", None)
    assert scores["img_004"]["key_status"] == "invalid_key"
    no_key = next(e for e in events_of(engine, "img_003") if e.type == "label_scored")
    assert "no key" in no_key.because
    assert {r["status"] for r in engine.export_items("wf")} == {"final"}
    assert engine.score_report("wf") == [
        {
            "workflow": "wf",
            "stage": "annotate",
            "score_against": "gold_key",
            "labeler": "ana",
            "labels": 4,
            "scored": 2,
            "no_key": 2,
            "agreed": 1,
            "fraction_agreed": 0.5,
        }
    ]


# --- release, exclude, cancel -----------------------------------------------------------


def test_release_returns_the_slot_and_the_releaser_is_not_offered_it_again(build, cfg):
    engine = build(cfg(ANNOTATE), items=2)
    slot = engine.request_work("wf", "ana")
    with pytest.raises(InvalidRequest, match="reason"):
        engine.release("wf", slot.slot, "ana", "")
    with pytest.raises(SlotNotYours):
        engine.release("wf", slot.slot, "ben", "not mine")
    engine.release("wf", slot.slot, "ana", "image will not load")
    released = engine.store.events()[-1]
    assert (released.type, released.reason) == ("slot_released", "image will not load")
    assert engine.slot("wf", slot.slot).status == "open"
    assert engine.request_work("wf", "ana").item == "img_002"
    assert engine.request_work("wf", "ben").item == "img_001"


def test_exclude_cancels_open_and_claimed_slots(build, cfg, work):
    engine = build(cfg(DOUBLE), items=2)
    claimed = engine.request_work("wf", "ana")
    assert claimed.item == "img_001"
    cancelled = engine.exclude_item("wf", "img_001", "admin", "image is corrupt")
    assert cancelled == [1, 2]
    assert {engine.slot("wf", s).status for s in cancelled} == {"cancelled"}
    assert final_row(engine, "img_001") is None
    assert engine.export_items("wf")[0]["status"] == "excluded"
    assert engine.request_work("wf", "ana").item == "img_002"
    with pytest.raises(NotAllowed, match="already excluded"):
        engine.exclude_item("wf", "img_001", "admin", "again")
    with pytest.raises(UnknownItem):
        engine.exclude_item("wf", "img_404", "admin", "no such item")


def test_a_finalized_item_cannot_be_excluded(build, cfg, work):
    engine = build(cfg(ANNOTATE), items=1)
    work(engine, "wf", "ana")
    with pytest.raises(NotAllowed, match="already has its final label"):
        engine.exclude_item("wf", "img_001", "admin", "too late")


def test_cancel_open_slots_cancels_only_unclaimed_slots_and_moves_items_on(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=3)
    work(engine, "wf", "ana", "cat")  # img_001 slot 1
    claimed = engine.request_work("wf", "ben")  # img_001 slot 2
    cancelled = engine.cancel_open_slots("wf", "annotate", "admin", "enough labels")
    assert cancelled == [3, 4, 5, 6]
    assert engine.slot("wf", claimed.slot).status == "claimed"
    # img_002 and img_003 lost every slot: they leave annotate with no labels, no final label.
    for item in ("img_002", "img_003"):
        left = next(e for e in events_of(engine, item) if e.type == "stage_completed")
        assert left.outputs == ()
        assert final_row(engine, item)["status"] == "none"
    with pytest.raises(UnknownStage, match="Did you mean 'annotate'"):
        engine.cancel_open_slots("wf", "anotate", "admin", "typo")
    assert engine.cancel_open_slots("wf", "annotate", "admin", "nothing left") == []


def test_a_stage_with_one_label_left_after_cancelling_gives_only_the_plain_output(build, cfg, work):
    review = {"name": "review", "input": "annotate", "assign_to": "expert"}
    engine = build(cfg(DOUBLE, review, final=["review", "annotate"]), items=1)
    work(engine, "wf", "ana", "cat")
    engine.cancel_open_slots("wf", "annotate", "admin", "one label is enough here")
    left = next(e for e in events_of(engine, "img_001") if e.type == "stage_completed")
    assert left.outputs == ("annotate",)
    assert engine.request_work("wf", "dr_smith").stage == "review"


def test_state_rebuilds_after_every_kind_of_command(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=4)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    slot = engine.request_work("wf", "ana")
    engine.release("wf", slot.slot, "ana", "break")
    engine.exclude_item("wf", "img_003", "admin", "blurry")
    engine.cancel_open_slots("wf", "annotate", "admin", "stop")
    work(engine, "wf", "dr_smith", "cat")
    assert rebuild(engine.store.events()).snapshot() == engine.store.snapshot()
