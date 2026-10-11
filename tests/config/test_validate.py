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


def test_a_valid_config_gives_an_ok_report_with_the_config():
    report = validate_config(REVIEW)
    assert report.ok
    assert report.errors == [] and report.warnings == []
    assert isinstance(report.config, WorkflowConfig)
    assert report.render() == "The config is valid.\n"


def test_a_parsed_dict_is_accepted_like_yaml_text():
    assert validate_config(document()).config == validate_config(REVIEW).config


def test_bytes_are_read_as_utf8_yaml():
    assert validate_config(REVIEW.encode()).ok


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


def test_unknown_key_suggestions_come_from_the_keys_of_that_level():
    top = validate_config(document(datset="x")).errors
    assert "Did you mean 'dataset'?" in top[0].message
    stage = validate_config(document(stages=[{"name": "a", "assign_to": "x", "show_prir": 1}]))
    assert "Did you mean 'show_prior'?" in stage.errors[0].message


def test_unknown_key_without_a_close_match_lists_the_allowed_keys():
    [problem] = validate_config(document(colour="red")).errors
    assert "workflow, dataset, label_task, stages, final" in problem.message


def test_unnamed_stages_are_called_by_their_position():
    data = document(
        stages=[{"assign_to": "annotator"}, {"name": "b", "assign_to": "x", "sample": 9}]
    )
    assert [p.where for p in validate_config(data).errors] == [
        "stage 1, key 'name'",
        "stage 'b', key 'sample'",
    ]


# --------------------------------------------------------------------------- YAML problems


def test_a_yaml_syntax_error_names_the_line():
    report = validate_config("workflow: a\ndataset: b\nlabel_task c@1\n")
    [problem] = report.errors
    assert problem.where.startswith("line ")
    assert "not valid YAML" in problem.message


def test_the_yaml_error_line_is_where_the_problem_is():
    text = "workflow: a\ndataset: b\nlabel_task: c@1\nstages:\n  - name: x\n    assign_to y\n"
    [problem] = validate_config(text).errors
    assert problem.where == "line 6"


def test_a_key_written_twice_is_an_error():
    text = REVIEW.replace("    sample: 0.2\n", "    sample: 0.2\n    sample: 0.3\n")
    [problem] = validate_config(text).errors
    assert problem.where == "line 12"
    assert "'sample' is written twice" in problem.message


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


def test_an_unknown_role_is_an_error_with_a_suggestion():
    data = document(stages=[{"name": "annotate", "assign_to": ["annotater", "expert"]}])
    [problem] = validate_config(data, CONTEXT).errors
    assert problem.where == "stage 'annotate', key 'assign_to'"
    assert "The role 'annotater' does not exist in this project." in problem.message
    assert "Did you mean 'annotator'?" in problem.message


def test_an_unknown_role_without_a_close_name_lists_the_roles():
    data = document(stages=[{"name": "annotate", "assign_to": "zzz"}])
    [problem] = validate_config(data, CONTEXT).errors
    assert "The roles are 'annotator', 'expert' and 'trainee'." in problem.message


def test_an_unknown_dataset_is_an_error_with_a_suggestion():
    [problem] = validate_config(document(dataset="xray_batch_2"), CONTEXT).errors
    assert problem.where == "top level, key 'dataset'"
    assert "Did you mean 'xray_batch_1'?" in problem.message


def test_an_unknown_label_task_is_an_error_with_a_suggestion():
    [problem] = validate_config(document(label_task="image_ontolgy@1"), CONTEXT).errors
    assert problem.where == "top level, key 'label_task'"
    assert "Did you mean 'image_ontology@1'?" in problem.message


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


def test_enough_holders_gives_no_warning():
    config = config_with({"assign_to": "annotator", "labels_per_item": 2})
    assert role_warnings(config, {"annotator": ["ana", "bo"]}) == []


def test_too_few_holders_warns_with_the_shortfall():
    config = config_with({"assign_to": "annotator", "labels_per_item": 3})
    [warning] = role_warnings(config, {"annotator": ["ana", "bo"]})
    assert warning.where == "stage 'annotate'"
    assert warning.message == (
        "Stage 'annotate' needs 3 different people to label each item, but only 2 people hold "
        "the role 'annotator'. Some items will wait for another person, because nobody labels "
        "the same item twice. Give the role to one more person, or lower `labels_per_item`."
    )


def test_one_holder_is_worded_in_the_singular():
    config = config_with({"assign_to": "expert", "labels_per_item": 2})
    [warning] = role_warnings(config, {"expert": ["eve"]})
    assert "only 1 person holds the role 'expert'" in warning.message


def test_nobody_holding_the_role_warns():
    config = config_with({"assign_to": "annotator"})
    [warning] = role_warnings(config, {"annotator": []})
    assert "nobody holds the role 'annotator'" in warning.message


def test_the_count_of_a_fraction_is_what_is_required():
    config = config_with(
        {"assign_to": "annotator", "labels_per_item": {"count": 3, "fraction": 0.2}}
    )
    [warning] = role_warnings(config, {"annotator": ["ana", "bo"]})
    assert "needs 3 different people for the 20% of items chosen to get 3 labels" in warning.message
    assert role_warnings(config, {"annotator": ["ana", "bo", "cy"]}) == []


def test_all_warns_only_when_nobody_holds_the_roles():
    config = config_with({"assign_to": "trainee", "labels_per_item": "all"})
    assert role_warnings(config, {"trainee": ["tia"]}) == []
    [warning] = role_warnings(config, {"trainee": []})
    assert "nobody holds it yet" in warning.message


def test_people_holding_several_of_the_roles_count_once():
    config = config_with({"assign_to": ["annotator", "expert"], "labels_per_item": 3})
    roles = {"annotator": ["ana", "bo"], "expert": ["bo", "eve"]}  # three people, not four
    assert role_warnings(config, roles) == []
    [warning] = role_warnings(config, {"annotator": ["ana", "bo"], "expert": ["bo"]})
    assert "only 2 people hold any of the roles 'annotator' and 'expert'" in warning.message


def test_a_role_that_does_not_exist_is_left_to_validate_config():
    config = config_with({"assign_to": "ghost", "labels_per_item": 3})
    assert role_warnings(config, {"annotator": ["ana"]}) == []


def test_role_holders_may_be_any_collection():
    config = config_with({"assign_to": "annotator", "labels_per_item": 2})
    assert role_warnings(config, {"annotator": {"ana", "bo"}}) == []
    assert role_warnings(config, {"annotator": ("ana",)}) != []


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
