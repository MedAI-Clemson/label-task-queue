"""validate_config: it collects every problem, never raises, and checks the project context."""

import pytest

from labelflow.engine.config import (
    ConfigReport,
    Problem,
    ValidationContext,
    WorkflowConfig,
    role_warnings,
    validate_config,
)

REVIEW = """\
workflow: xray_main
dataset: xray_batch_1
label_task: image_ontology@1

stages:
  - name: annotate
    assign_to: annotator
  - name: expert_review
    input: annotate
    assign_to: expert
    sample: 0.2

final: [expert_review, annotate]
"""


def document(**changes):
    """The review example as a parsed dict, with some keys replaced (None removes one).

    When the stages are replaced, `final` is dropped too (it would name the old stages),
    unless it is given.
    """
    data = {
        "workflow": "xray_main",
        "dataset": "xray_batch_1",
        "label_task": "image_ontology@1",
        "stages": [
            {"name": "annotate", "assign_to": "annotator"},
            {"name": "expert_review", "input": "annotate", "assign_to": "expert", "sample": 0.2},
        ],
        "final": ["expert_review", "annotate"],
    }
    if "stages" in changes and "final" not in changes:
        data.pop("final")
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


# ------------------------------------------------------------------------ the report


@pytest.mark.parametrize(
    "source", [REVIEW, REVIEW.encode(), document()], ids=["text", "bytes", "dict"]
)
def test_a_valid_config_gives_an_ok_report_with_the_config(source):
    report = validate_config(source)
    assert report.ok
    assert report.errors == [] and report.warnings == []
    assert report.config == WorkflowConfig.model_validate(document())
    assert report.render() == "The config is valid.\n"


def test_an_invalid_config_has_no_config_and_is_not_ok():
    report = validate_config(document(dataset=None))
    assert not report.ok
    assert report.config is None
    assert [p.where for p in report.errors] == ["top level, key 'dataset'"]


def test_render_lists_errors_with_their_location():
    text = validate_config(document(dataset=None, label_task="nope")).render()
    assert text.startswith("The config has 2 errors. Fix them and check again.")
    assert "1. top level, key 'dataset'" in text
    assert "2. top level, key 'label_task'" in text


def test_problem_prints_as_where_and_message():
    assert str(Problem("top level", "Something is wrong.")) == "top level: Something is wrong."


@pytest.mark.parametrize(
    "source",
    [None, 5, 1.5, [], (), b"\xff\xfe", "", "   ", "# only a comment", "[", "a: b: c", "- 1", "5"],
)
def test_odd_input_never_raises(source):
    report = validate_config(source)
    assert isinstance(report, ConfigReport)
    assert not report.ok
    assert report.render().strip()


# --------------------------------------------------------------------- collecting problems


def test_all_problems_are_collected_not_just_the_first():
    data = document(
        workflow="not valid",
        stages=[
            {"name": "annotate", "assign_to": "annotator", "smaple": 2},
            {"name": "review", "input": "anotate", "assign_to": "expert", "sample": 3},
        ],
        final=["annotate", "nothing"],
    )
    wheres = [p.where for p in validate_config(data).errors]
    assert wheres == [
        "top level, key 'workflow'",
        "stage 'annotate', key 'smaple'",
        "stage 'review', key 'sample'",
        "stage 'review', key 'input'",
        "top level, key 'final'",
    ]


def test_graph_rules_still_run_when_a_field_is_wrong_elsewhere():
    data = document(
        stages=[
            {"name": "annotate", "assign_to": "annotator", "sample": "lots"},
            {"name": "review", "input": "anotate", "assign_to": "expert"},
        ]
    )
    wheres = [p.where for p in validate_config(data).errors]
    assert wheres == ["stage 'annotate', key 'sample'", "stage 'review', key 'input'"]


def test_a_mistake_in_a_stage_does_not_cause_follow_on_graph_errors():
    # `lables_per_item` is a typo, so `annotate` may well have several labels:
    # complaining that annotate.agreed is always empty would be a false alarm.
    data = document(
        stages=[
            {"name": "annotate", "assign_to": "annotator", "lables_per_item": 2},
            {"name": "adjudicate", "input": "annotate.disagreed", "assign_to": "expert"},
        ],
        final=["adjudicate", "annotate"],
    )
    problems = validate_config(data).errors
    assert [p.where for p in problems] == ["stage 'annotate', key 'lables_per_item'"]
    assert "Did you mean 'labels_per_item'?" in problems[0].message


def test_coverage_is_only_checked_when_everything_else_is_clean():
    data = document(
        stages=[
            {"name": "annotate", "assign_to": "annotator", "smaple": 1},
            {"name": "review", "input": "annotate", "assign_to": "expert", "sample": 0.2},
        ],
        final=["review"],
    )
    assert [p.where for p in validate_config(data).errors] == ["stage 'annotate', key 'smaple'"]


def test_a_typo_for_a_required_key_is_reported_once():
    data = document(stages=[{"name": "annotate", "assing_to": "annotator"}])
    [problem] = validate_config(data).errors
    assert "Did you mean 'assign_to'?" in problem.message


def test_unnamed_stages_are_called_by_their_position():
    data = document(
        stages=[{"assign_to": "annotator"}, {"name": "b", "assign_to": "x", "sample": 9}]
    )
    assert [p.where for p in validate_config(data).errors] == [
        "stage 1, key 'name'",
        "stage 'b', key 'sample'",
    ]


# ------------------------------------------------------------------- the project context


ROLES = {"annotator": ["ana", "bo"], "expert": ["eve"], "trainee": []}
CONTEXT = ValidationContext(
    roles=ROLES, datasets=["xray_batch_1"], label_tasks=["image_ontology@1", "image_ontology@2"]
)


def test_a_config_that_matches_the_project_has_no_errors():
    report = validate_config(REVIEW, CONTEXT)
    assert report.ok
    assert report.warnings == []


def test_without_a_context_nothing_is_checked_against_the_project():
    assert validate_config(document(dataset="anything"), None).ok
    assert validate_config(document(dataset="anything"), ValidationContext()).ok


@pytest.mark.parametrize(
    ("changes", "where", "said"),
    [
        (
            {"stages": [{"name": "annotate", "assign_to": ["annotater", "expert"]}]},
            "stage 'annotate', key 'assign_to'",
            "The role 'annotater' does not exist in this project. Did you mean 'annotator'?",
        ),
        (
            {"stages": [{"name": "annotate", "assign_to": "zzz"}]},
            "stage 'annotate', key 'assign_to'",
            "The roles are 'annotator', 'expert' and 'trainee'.",
        ),
        (
            {"dataset": "xray_batch_2"},
            "top level, key 'dataset'",
            "The dataset 'xray_batch_2' does not exist in this project. "
            "Did you mean 'xray_batch_1'?",
        ),
        (
            {"label_task": "image_ontolgy@1"},
            "top level, key 'label_task'",
            "Did you mean 'image_ontology@1'?",
        ),
    ],
    ids=["role_with_suggestion", "role_listing_the_roles", "dataset", "label_task"],
)
def test_a_name_the_project_does_not_have_is_an_error(changes, where, said):
    [problem] = validate_config(document(**changes), CONTEXT).errors
    assert problem.where == where
    assert said in problem.message


def test_a_project_with_nothing_defined_says_so():
    context = ValidationContext(roles={}, datasets=[], label_tasks=[])
    messages = [p.message for p in validate_config(REVIEW, context).errors]
    assert any("The project has no roles yet." in m for m in messages)
    assert any("The project has no datasets yet." in m for m in messages)
    assert any("The project has no label tasks yet." in m for m in messages)


def test_context_errors_are_reported_once_the_config_itself_is_valid():
    data = document(dataset="nope", stages=[{"name": "a", "assign_to": "x", "sample": 7}])
    wheres = [p.where for p in validate_config(data, CONTEXT).errors]
    assert wheres == ["stage 'a', key 'sample'"]


def test_role_warnings_go_into_the_report_and_do_not_make_it_fail():
    roles = {"annotator": ["ana"], "expert": ["eve"]}
    text = REVIEW.replace("assign_to: annotator", "assign_to: annotator\n    labels_per_item: 2")
    report = validate_config(text, ValidationContext(roles=roles))
    assert report.ok
    [warning] = report.warnings
    assert warning.where == "stage 'annotate'"
    assert report.render().startswith("The config is valid. It has 1 warning.")
    assert "Warnings:" in report.render()


def test_an_unknown_role_is_an_error_and_not_also_a_warning():
    data = document(stages=[{"name": "a", "assign_to": "zzz", "labels_per_item": 3}])
    report = validate_config(data, ValidationContext(roles=ROLES))
    assert len(report.errors) == 1
    assert report.warnings == []


# --------------------------------------------------------------------------- role warnings


def config_with(stage_keys):
    """A one-stage config; the stage keys are those of the stage."""
    return WorkflowConfig.model_validate(
        {
            "workflow": "w",
            "dataset": "d",
            "label_task": "t@1",
            "stages": [{"name": "annotate", **stage_keys}],
        }
    )


FRACTION = {"count": 3, "fraction": 0.2}


@pytest.mark.parametrize(
    ("stage_keys", "roles"),
    [
        ({"assign_to": "annotator", "labels_per_item": 2}, {"annotator": ["ana", "bo"]}),
        ({"assign_to": "annotator", "labels_per_item": FRACTION}, {"annotator": ["a", "b", "c"]}),
        ({"assign_to": "trainee", "labels_per_item": "all"}, {"trainee": ["tia"]}),
        # people holding several of the roles count once: three people, not four
        (
            {"assign_to": ["annotator", "expert"], "labels_per_item": 3},
            {"annotator": ["ana", "bo"], "expert": ["bo", "eve"]},
        ),
        # a role that does not exist is left to validate_config, which reports it as an error
        ({"assign_to": "ghost", "labels_per_item": 3}, {"annotator": ["ana"]}),
        # the holders may be any collection
        ({"assign_to": "annotator", "labels_per_item": 2}, {"annotator": {"ana", "bo"}}),
    ],
    ids=["enough", "fraction_count", "all", "shared_holders", "unknown_role", "a_set"],
)
def test_enough_holders_gives_no_warning(stage_keys, roles):
    assert role_warnings(config_with(stage_keys), roles) == []


@pytest.mark.parametrize(
    ("stage_keys", "roles", "said"),
    [
        (
            {"assign_to": "annotator", "labels_per_item": 3},
            {"annotator": ["ana", "bo"]},
            "Stage 'annotate' needs 3 different people to label each item, but only 2 people "
            "hold the role 'annotator'. Some items will wait for another person, because nobody "
            "labels the same item twice. Give the role to one more person, or lower "
            "`labels_per_item`.",
        ),
        (
            {"assign_to": "expert", "labels_per_item": 2},
            {"expert": ["eve"]},
            "only 1 person holds the role 'expert'",
        ),
        (
            {"assign_to": "annotator"},
            {"annotator": []},
            "nobody holds the role 'annotator'",
        ),
        (
            {"assign_to": "annotator", "labels_per_item": FRACTION},
            {"annotator": ["ana", "bo"]},
            "needs 3 different people for the 20% of items chosen to get 3 labels",
        ),
        (
            {"assign_to": "trainee", "labels_per_item": "all"},
            {"trainee": []},
            "nobody holds it yet",
        ),
        (
            {"assign_to": ["annotator", "expert"], "labels_per_item": 3},
            {"annotator": ["ana", "bo"], "expert": ["bo"]},
            "only 2 people hold any of the roles 'annotator' and 'expert'",
        ),
        (
            {"assign_to": "annotator", "labels_per_item": 2},
            {"annotator": ("ana",)},
            "only 1 person holds",
        ),
    ],
    ids=["shortfall", "singular", "nobody", "fraction_count", "all", "shared_holders", "a_tuple"],
)
def test_too_few_holders_warns(stage_keys, roles, said):
    [warning] = role_warnings(config_with(stage_keys), roles)
    assert warning.where == "stage 'annotate'"
    assert said in warning.message


def test_each_stage_is_checked_in_order():
    config = WorkflowConfig.model_validate(
        {
            "workflow": "w",
            "dataset": "d",
            "label_task": "t@1",
            "stages": [
                {"name": "first", "assign_to": "a", "labels_per_item": 2},
                {"name": "second", "input": "first", "assign_to": "b", "labels_per_item": 2},
            ],
        }
    )
    warnings = role_warnings(config, {"a": ["x"], "b": ["y"]})
    assert [w.where for w in warnings] == ["stage 'first'", "stage 'second'"]
