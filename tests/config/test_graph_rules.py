"""The graph rules, as enforced by WorkflowConfig itself: a config that exists is valid."""

import pytest
from pydantic import ValidationError

from labelflow.engine.config import ConfigGraphError, WorkflowConfig


def make(stages, final=None):
    """Build a config from a list of stages written as (name, extra keys) pairs."""
    document = {
        "workflow": "w",
        "dataset": "d",
        "label_task": "task@1",
        "stages": [{"name": name, "assign_to": "role", **extra} for name, extra in stages],
    }
    if final is not None:
        document["final"] = final
    return WorkflowConfig.model_validate(document)


def problems_of(stages, final=None):
    """The problems the graph rules report for these stages."""
    with pytest.raises(ValidationError) as caught:
        make(stages, final)
    [error] = caught.value.errors()
    cause = error["ctx"]["error"]
    assert isinstance(cause, ConfigGraphError)
    return cause.problems


def test_a_valid_workflow_is_accepted():
    config = make(
        [("annotate", {}), ("review", {"input": "annotate", "sample": 0.2})],
        final=["review", "annotate"],
    )
    assert config.stage_names == ("annotate", "review")


def test_the_error_is_a_value_error_with_plain_text():
    with pytest.raises(ValueError, match="Stage names must be unique"):
        make([("a", {}), ("a", {})])


def test_stage_names_must_be_unique():
    [problem] = problems_of([("a", {}), ("b", {}), ("a", {})])
    assert problem.where == "stage 'a', key 'name'"
    assert "used by stages 1 and 3" in problem.message


def test_input_may_name_an_earlier_stage():
    make([("a", {}), ("b", {"input": "a"}), ("c", {"input": "a"}), ("d", {"input": "c"})])


@pytest.mark.parametrize(
    ("stages", "said"),
    [
        (
            [("annotate", {}), ("review", {"input": "anotate"})],
            "no stage listed before it has that name. Did you mean 'annotate'?",
        ),
        (
            [("annotate", {}), ("review", {"input": "zzz"})],
            "that name. The stages listed before it are 'annotate'.",
        ),
        (
            [("review", {"input": "annotate"}), ("annotate", {})],
            "'annotate' is listed after 'review'. Stages are listed in order",
        ),
        ([("annotate", {}), ("review", {"input": "review"})], "takes its items from itself"),
        (
            [("review", {"input": "annotate"})],
            "no stage is listed before it. To take every item of the dataset, leave `input` out.",
        ),
    ],
    ids=["misspelled", "unknown", "listed_later", "itself", "first_stage"],
)
def test_input_must_name_an_earlier_stage(stages, said):
    [problem] = problems_of(stages)
    assert problem.where == "stage 'review', key 'input'"
    assert said in problem.message


@pytest.mark.parametrize("outcome", ["agreed", "disagreed"])
@pytest.mark.parametrize("labels", [1, None])  # None leaves labels_per_item at its default, 1
def test_agreed_and_disagreed_need_more_than_one_label(outcome, labels):
    upstream = {} if labels is None else {"labels_per_item": labels}
    [problem] = problems_of([("a", upstream), ("b", {"input": f"a.{outcome}"})])
    assert problem.where == "stage 'b', key 'input'"
    assert "would always be empty" in problem.message


@pytest.mark.parametrize("labels", [2, 3, "all", {"count": 2, "fraction": 0.3}])
def test_agreed_and_disagreed_are_allowed_when_items_can_have_several_labels(labels):
    make(
        [
            ("a", {"labels_per_item": labels}),
            ("b", {"input": "a.agreed"}),
            ("c", {"input": "a.disagreed"}),
        ]
    )


def test_final_names_must_be_stages_and_distinct():
    problems = problems_of([("a", {})], final=["a", "a", "x"])
    messages = " ".join(problem.message for problem in problems)
    assert "'a' more than once" in messages
    assert "'x', but there is no stage" in messages


def test_final_may_not_be_empty():
    [problem] = problems_of([("a", {})], final=[])
    assert "empty list" in problem.message
    assert "final: none" in problem.message


def test_final_none_skips_the_coverage_rule():
    config = make([("a", {}), ("b", {"input": "a", "sample": 0.1})], final="none")
    assert config.resolved_final() is None


def test_omitted_final_means_every_stage_so_coverage_holds():
    config = make([("a", {}), ("b", {"input": "a", "sample": 0.1})])
    assert config.resolved_final() == ("b", "a")


def test_a_sampled_stage_downstream_does_not_cover_its_input():
    [problem] = problems_of(
        [("annotate", {}), ("expert_review", {"input": "annotate", "sample": 0.2})],
        final=["expert_review"],
    )
    assert problem.message == (
        "Stage 'annotate' is not listed in `final`, and not all of its items go on to a later "
        "stage (expert_review takes only a 20% sample), so some items would finish with no "
        "final label. Add 'annotate' to `final`."
    )


def test_a_downstream_stage_taking_everything_covers_its_input():
    make([("a", {}), ("b", {"input": "a"})], final=["b"])


def test_both_sides_of_agreement_cover_a_stage_labeled_several_times():
    make(
        [
            ("a", {"labels_per_item": 2}),
            ("agreed_check", {"input": "a.agreed"}),
            ("adjudicate", {"input": "a.disagreed"}),
        ],
        final=["agreed_check", "adjudicate"],
    )


@pytest.mark.parametrize(
    "stages",
    [
        # only the disagreed side goes on
        [("a", {"labels_per_item": 2}), ("b", {"input": "a.disagreed"})],
        # one side is sampled
        [
            ("a", {"labels_per_item": 2}),
            ("b", {"input": "a.disagreed", "sample": 0.5}),
            ("c", {"input": "a.agreed"}),
        ],
        # labels_per_item "all" may give an item only one label, which has no outcome
        [
            ("a", {"labels_per_item": "all"}),
            ("b", {"input": "a.disagreed"}),
            ("c", {"input": "a.agreed"}),
        ],
    ],
)
def test_agreement_split_that_does_not_cover_everything_is_refused(stages):
    names = [name for name, extra in stages if "input" in extra]
    problems = problems_of(stages, final=names)
    assert [problem.where for problem in problems] == ["stage 'a'"]


# --- Parallel stages sharing a role, with labels_per_item: all ----------------------------


def test_two_root_stages_sharing_a_role_may_not_both_label_by_everyone():
    [problem] = problems_of(
        [
            ("a", {"assign_to": "expert", "labels_per_item": "all"}),
            ("b", {"assign_to": "expert", "labels_per_item": "all"}),
        ],
        final="none",
    )
    assert problem.where == "stage 'a', key 'labels_per_item'"
    assert problem.message == (
        "Stages 'a' and 'b' can both hold the same item at the same time (both take every item "
        "of the dataset), and both are assigned to the role 'expert'. Both have "
        "`labels_per_item: all`, which reserves a slot in each stage for every holder of the "
        "role 'expert', but nobody labels an item twice in a workflow, so each person can fill "
        "only one of their two slots, and the item would stay in progress forever. Give the two "
        "stages different roles, or chain them so that one takes the other's output "
        "(`input: a` on 'b')."
    )


def test_one_all_stage_in_parallel_with_an_ordinary_stage_on_the_same_role():
    [problem] = problems_of(
        [
            ("annotate", {"assign_to": "annotator"}),
            ("check", {"input": "annotate", "assign_to": ["expert", "lead"]}),
            ("review", {"input": "annotate", "assign_to": "expert", "labels_per_item": "all"}),
        ],
        final=["review", "check", "annotate"],
    )
    assert problem.where == "stage 'review', key 'labels_per_item'"
    assert problem.message == (
        "Stages 'check' and 'review' can both hold the same item at the same time (both take "
        "their items from 'annotate'), and both are assigned to the role 'expert'. 'review' "
        "has `labels_per_item: all`, which reserves a slot for every holder of the role "
        "'expert', but nobody labels an item twice in a workflow, so anyone who labels the "
        "item in 'check' first can never fill their slot in 'review', and the item would stay "
        "in progress forever. Give the two stages different roles, or chain them so that one "
        "takes the other's output (`input: check` on 'review')."
    )


def test_parallel_stages_further_apart_in_the_graph_still_conflict():
    # 'audit' takes the output of 'b', which takes 'a'; 'a' and 'audit' hold items together
    [problem] = problems_of(
        [
            ("a", {"assign_to": "expert", "labels_per_item": "all"}),
            ("b", {"assign_to": "annotator"}),
            ("audit", {"input": "b", "assign_to": ["expert", "lead"]}),
        ],
        final="none",
    )
    assert problem.where == "stage 'a', key 'labels_per_item'"
    assert problem.message.startswith(
        "Stages 'a' and 'audit' can both hold the same item at the same time (neither takes its "
        "items from the other), and both are assigned to the role 'expert'."
    )


def test_several_shared_roles_are_all_named():
    [problem] = problems_of(
        [
            ("a", {"assign_to": ["expert", "lead", "x"], "labels_per_item": "all"}),
            ("b", {"assign_to": ["lead", "expert"]}),
        ],
        final="none",
    )
    assert "both are assigned to the roles 'expert' and 'lead'" in problem.message


@pytest.mark.parametrize(
    "stages",
    [
        # the agreed and disagreed sides of one stage never hold the same item
        [
            ("annotate", {"labels_per_item": 2}),
            (
                "confirm",
                {"input": "annotate.agreed", "assign_to": "expert", "labels_per_item": "all"},
            ),
            (
                "adjudicate",
                {"input": "annotate.disagreed", "assign_to": "expert", "labels_per_item": "all"},
            ),
        ],
        # ...nor do stages further down each side
        [
            ("annotate", {"labels_per_item": 2}),
            ("confirm", {"input": "annotate.agreed", "assign_to": "x"}),
            ("audit", {"input": "confirm", "assign_to": "expert", "labels_per_item": "all"}),
            ("adjudicate", {"input": "annotate.disagreed", "assign_to": "expert"}),
        ],
        # chained stages are not parallel: the item leaves 'a' before it reaches 'b'
        [
            ("a", {"assign_to": "expert", "labels_per_item": "all"}),
            ("b", {"input": "a", "assign_to": "expert"}),
        ],
        [
            ("a", {"assign_to": "expert"}),
            ("b", {"input": "a", "assign_to": "x"}),
            ("c", {"input": "b", "assign_to": "expert", "labels_per_item": "all"}),
        ],
        # different roles never conflict
        [
            ("a", {"assign_to": "expert", "labels_per_item": "all"}),
            ("b", {"assign_to": "annotator", "labels_per_item": "all"}),
        ],
        # a shared role without `all` is a staffing question (a warning), not an error
        [
            ("a", {"assign_to": "expert", "labels_per_item": 2}),
            ("b", {"assign_to": "expert", "labels_per_item": {"count": 3, "fraction": 0.5}}),
        ],
    ],
    ids=[
        "agreed_and_disagreed",
        "downstream_of_a_split",
        "chained",
        "chained_further",
        "different_roles",
        "no_all",
    ],
)
def test_stages_that_cannot_hold_an_item_together_may_share_a_role(stages):
    make(stages, final="none")


def test_parallel_stages_are_listed_as_pairs():
    from labelflow.engine.config.graph import parallel_stages

    config = make(
        [
            ("annotate", {"labels_per_item": 2}),
            ("review", {"input": "annotate", "assign_to": "x", "sample": 0.2}),
            ("confirm", {"input": "annotate.agreed", "assign_to": "y"}),
            ("adjudicate", {"input": "annotate.disagreed", "assign_to": "z"}),
            ("audit", {"input": "adjudicate", "assign_to": "w"}),
        ],
        final=["audit", "review", "confirm", "annotate"],
    )
    assert parallel_stages(config.model_dump()) == [
        ("review", "confirm"),
        ("review", "adjudicate"),
        ("review", "audit"),
    ]


def test_the_parallel_rule_waits_until_the_rest_of_the_document_is_clean():
    # with an input problem, the graph cannot be trusted, so only that problem is reported
    [problem] = problems_of(
        [
            ("a", {"assign_to": "expert", "labels_per_item": "all"}),
            ("b", {"input": "nope", "assign_to": "expert", "labels_per_item": "all"}),
        ],
        final="none",
    )
    assert problem.where == "stage 'b', key 'input'"


def test_all_problems_are_reported_together():
    problems = problems_of(
        [("b", {"input": "a"}), ("a", {}), ("a", {"input": "a.agreed"})], final=["b", "b"]
    )
    assert len(problems) >= 3
