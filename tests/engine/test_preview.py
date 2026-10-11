"""The change preview: a pure read, a readable text, a diff, and counts that add up.

``test_edits.py`` checks the counts on the design note's example workflows; this module
checks how they are defined at the edges: items with one slot never reach ``.agreed``, a
new stage without input reaches only items imported later, and every item is counted once.
"""

from ltq.engine import draw

ANNOTATE = {"name": "annotate", "assign_to": "annotator"}
REVIEW = {"name": "expert_review", "input": "annotate", "assign_to": "expert", "sample": 0.2}
DOUBLE = {"name": "annotate", "assign_to": "annotator", "labels_per_item": 2}
ADJUDICATE = {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"}


def test_a_preview_changes_nothing(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=3)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    events, state = engine.store.events(), engine.store.snapshot()
    proposals = [
        cfg(DOUBLE, ADJUDICATE, final=["annotate", "adjudicate"]),
        cfg({**DOUBLE, "labels_per_item": 3}, final=["annotate"]),
        cfg(ANNOTATE, final=["annotate"]) | {"dataset": "other"},
        "workflow: wf\nstages: [",
    ]
    for proposal in proposals:
        engine.preview_edit("wf", proposal)
    assert engine.store.events() == events
    assert engine.store.snapshot() == state


def test_the_preview_reads_plainly(build, cfg, work):
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=3)
    work(engine, "wf", "ana", "cat")
    work(engine, "wf", "ben", "dog")
    preview = engine.preview_edit("wf", cfg(DOUBLE, ADJUDICATE, final=["annotate", "adjudicate"]))
    assert preview.render() == (
        "Change preview for workflow 'wf': version 1 (active) -> version 2 (proposed)\n"
        "\n"
        "This edit can be activated. It makes 1 change, all applying in place.\n"
        "\n"
        "Changes:\n"
        "  1. final: [adjudicate, annotate] -> [annotate, adjudicate]  [applies in place]\n"
        "     Items finalized after activation take their final label from the first of "
        "annotate,\n"
        "     adjudicate that labeled them; items already finalized keep theirs.\n"
        "\n"
        "What it touches. The workflow has 3 items: 3 in progress, 0 finished, 0 excluded. "
        "Items\n"
        "imported later always follow the version in force when they arrive.\n"
        "\n"
        "  Final step (changed):\n"
        "    - Not finalized yet: 3 items. Their final labels will follow version 2.\n"
        "    - Unaffected: 0 items already finalized (they keep their final labels) and 0 "
        "items\n"
        "      excluded (no final label).\n"
        "\n"
        "Diff of the resolved configs:\n"
        "--- version 1 (active)\n"
        "+++ version 2 (proposed)\n"
        "@@ -20,4 +20,4 @@\n"
        "     score_against: null             # labels are not scored against a key\n"
        "     show_prior: true                # labelers see earlier labels\n"
        " \n"
        "-final: [adjudicate, annotate]       # the first listed stage that labeled an item "
        "gives its label\n"
        "+final: [annotate, adjudicate]       # the first listed stage that labeled an item "
        "gives its label\n"
    )


def test_a_sample_change_reads_with_its_draws(build, cfg, work):
    engine = build(cfg(ANNOTATE, REVIEW, final=["expert_review", "annotate"]), items=8, seed=50)
    for user in ("ana", "ben", "ana", "ben"):
        work(engine, "wf", user)
    preview = engine.preview_edit(
        "wf", cfg(ANNOTATE, {**REVIEW, "sample": 0.3}, final=["expert_review", "annotate"])
    )
    text = preview.render()
    assert "  1. expert_review.sample: 0.2 -> 0.3  [applies in place]\n" in text
    assert "  Stage 'expert_review' (changed):\n" in text
    assert "    - Slots now: 1 open and 0 claimed. They stay as they are.\n" in text
    assert (
        "    - May still arrive: 4 items in progress, decided under version 2 if they do.\n" in text
    )
    # img_005 to img_008 are still in annotate; by their draws, the old sample would admit
    # img_006, and the new one img_006 and img_007.
    arriving = [f"img_00{i}" for i in range(5, 9)]
    assert sum(draw(50, "expert_review", i) < 0.2 for i in arriving) == 1
    assert sum(draw(50, "expert_review", i) < 0.3 for i in arriving) == 2
    assert "if they arrive: 1 at the old sample 0.2, 2 at the new 0.3." in " ".join(text.split())
    assert "+    sample: 0.3                     # 30% of arriving items enter" in text


def test_a_refused_edit_reads_with_its_reasons(build, cfg):
    engine = build(cfg(ANNOTATE, REVIEW, final=["expert_review", "annotate"]), items=2)
    preview = engine.preview_edit(
        "wf", cfg(ANNOTATE, REVIEW, final=["expert_review", "annotate"]) | {"dataset": "other"}
    )
    text = preview.render()
    assert "This edit cannot be activated: 1 problem (see Refused).\n" in text
    assert "  1. dataset: ds -> other  [refused]\n" in text
    assert "\nRefused:\n  - Changing the dataset from 'ds' to 'other' needs a new workflow" in text


def test_a_proposal_may_be_yaml_text(build, cfg):
    engine = build(cfg(ANNOTATE, REVIEW, final=["expert_review", "annotate"]), items=2)
    text = """
workflow: wf
dataset: ds
label_task: standin@1
stages:
  - name: annotate
    assign_to: annotator
  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.5
final: [expert_review, annotate]
"""
    preview = engine.preview_edit("wf", text)
    assert preview.ok and [c.summary for c in preview.changes] == [
        "expert_review.sample: 0.2 -> 0.5"
    ]
    engine.activate_edit("wf", text, "admin", base_version=1)
    assert engine.store.get("workflows", workflow="wf")["active_version"] == 2

    broken = engine.preview_edit("wf", text.replace("sample: 0.5", "sample: [0.5"))
    assert not broken.ok and "not valid YAML" in broken.errors[0]
    assert (broken.changes, broken.stages, broken.diff) == ((), (), "")


def test_only_items_with_two_or_more_slots_may_reach_an_agreed_output(build, cfg):
    """With ``{count: 2, fraction: 0.5}``, half the items (by their own draw) get 2 slots.
    Only those can ever agree, so only they may arrive at a stage on ``annotate.agreed``."""
    some_double = {**ANNOTATE, "labels_per_item": {"count": 2, "fraction": 0.5}}
    old = cfg(some_double, ADJUDICATE, final=["adjudicate", "annotate"])
    engine = build(old, items=8)
    two_slots = {
        f"img_00{i}"
        for i in range(1, 9)
        if draw(1234, "annotate", f"img_00{i}", "labels_per_item") < 0.5
    }
    assert two_slots == {"img_002", "img_005", "img_007", "img_008"}
    check = {"name": "agreed_check", "input": "annotate.agreed", "assign_to": "expert"}
    preview = engine.preview_edit(
        "wf",
        cfg(some_double, ADJUDICATE, check, final=["adjudicate", "agreed_check", "annotate"]),
    )
    [impact] = preview.stages
    assert (impact.may_arrive, impact.cannot_arrive) == (4, 4)


def test_a_new_stage_without_input_reaches_only_items_imported_later(build, cfg, work):
    engine = build(cfg(ANNOTATE), items=3)
    triage = {"name": "triage", "assign_to": "expert"}
    new = cfg(ANNOTATE, triage, final=["triage", "annotate"])
    preview = engine.preview_edit("wf", new)
    stage_added = preview.changes[0]
    assert stage_added.effect == (
        "Items imported after activation arrive at 'triage'; items already in the workflow do "
        "not (no backfill)."
    )
    [impact] = preview.stages
    assert (impact.may_arrive, impact.cannot_arrive) == (0, 3)
    engine.activate_edit("wf", new, "admin", base_version=1)
    engine.import_item("ds", "late", "file:///late.png", "sha-late", {}, "admin")
    arrivals = engine.store.select("admissions", workflow="wf", stage="triage")
    assert [(r["item"], r["config_version"]) for r in arrivals] == [("late", 2)]


def test_counts_follow_a_chain_of_new_stages(build, cfg, work):
    engine = build(cfg(ANNOTATE), items=4)
    work(engine, "wf", "ana")  # img_001 leaves annotate: too late for the new stages
    first = {"name": "first_check", "input": "annotate", "assign_to": "expert"}
    second = {"name": "second_check", "input": "first_check", "assign_to": "expert"}
    preview = engine.preview_edit(
        "wf", cfg(ANNOTATE, first, second, final=["second_check", "first_check", "annotate"])
    )
    assert [(s.stage, s.status, s.may_arrive, s.cannot_arrive) for s in preview.stages] == [
        ("first_check", "added", 3, 1),
        ("second_check", "added", 3, 1),
    ]


def test_every_item_is_counted_once(build, cfg, work):
    """At every step of a run, each item is in exactly one of the four groups."""
    engine = build(cfg(DOUBLE, ADJUDICATE, final=["adjudicate", "annotate"]), items=6)
    engine.exclude_item("wf", "img_006", "admin", "corrupt")
    second = {"name": "second_look", "input": "annotate.agreed", "assign_to": "expert"}
    proposal = cfg(
        DOUBLE,
        {**ADJUDICATE, "show_prior": False},
        second,
        final=["adjudicate", "second_look", "annotate"],
    )
    users = [("ana", "cat"), ("ben", "dog"), ("dr_smith", "cat"), ("ben", "cat")]
    for step in range(12):
        preview = engine.preview_edit("wf", proposal)
        assert preview.ok
        for impact in preview.stages:
            groups = impact.in_stage + impact.may_arrive + impact.past + impact.cannot_arrive
            assert groups == preview.items.entered == 6
        items = preview.items
        assert items.in_progress + items.finished + items.excluded == items.entered
        user, value = users[step % len(users)]
        work(engine, "wf", user, value)


def test_a_new_assign_to_warns_about_slots_reserved_for_people_without_its_roles(build, cfg):
    grants = {"t1": ("trainee",), "t2": ("trainee", "annotator"), "ana": ("annotator",)}
    gold = {"name": "gold_read", "assign_to": "trainee", "labels_per_item": "all"}
    engine = build(cfg(gold, final="none"), items=1, grants=grants)
    preview = engine.preview_edit("wf", cfg({**gold, "assign_to": "annotator"}, final="none"))
    assert preview.ok
    assert preview.warnings == (
        "1 open slot of 'gold_read' is reserved for t1 (labels_per_item: all), who holds none "
        "of its new roles (annotator). Such a slot waits until its person is given one of the "
        "roles, or until it is cancelled.",
    )
