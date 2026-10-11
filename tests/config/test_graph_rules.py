"""The graph rules, as enforced by WorkflowConfig itself: a config that exists is valid."""

import pytest
from pydantic import ValidationError

from ltq.engine.config import ConfigGraphError, WorkflowConfig


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


def test_input_unknown_stage_suggests_a_close_name():
    [problem] = problems_of([("annotate", {}), ("review", {"input": "anotate"})])
    assert problem.where == "stage 'review', key 'input'"
    assert "Did you mean 'annotate'?" in problem.message


def test_input_unknown_stage_without_a_close_name_lists_the_earlier_stages():
    [problem] = problems_of([("annotate", {}), ("review", {"input": "zzz"})])
    assert "Did you mean" not in problem.message
    assert "'annotate'" in problem.message


def test_input_listed_later_says_stages_are_listed_in_order():
    [problem] = problems_of([("review", {"input": "annotate"}), ("annotate", {})])
    assert "'annotate' is listed after 'review'" in problem.message
    assert "Stages are listed in order" in problem.message


def test_input_may_not_be_the_stage_itself():
    [problem] = problems_of([("a", {}), ("b", {"input": "b"})])
    assert "from itself" in problem.message


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


def test_all_problems_are_reported_together():
    problems = problems_of(
        [("b", {"input": "a"}), ("a", {}), ("a", {"input": "a.agreed"})], final=["b", "b"]
    )
    assert len(problems) >= 3
