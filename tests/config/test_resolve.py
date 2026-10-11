"""The resolved view: every default filled in, and readable back in."""

import json
from pathlib import Path

import pytest
import yaml

from labelflow.engine.config import WorkflowConfig, resolve, resolved_yaml, validate_config

TESTS_DIR = Path(__file__).parent / "resolved"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "workflows"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.yaml"))


def make(stages, **top):
    document = {"workflow": "w", "dataset": "d", "label_task": "t@1", "stages": stages, **top}
    return WorkflowConfig.model_validate(document)


def test_defaults_are_filled_in():
    config = make([{"name": "annotate", "assign_to": "annotator"}])
    assert resolve(config) == {
        "workflow": "w",
        "dataset": "d",
        "label_task": "t@1",
        "stages": [
            {
                "name": "annotate",
                "input": None,
                "assign_to": ["annotator"],
                "sample": 1.0,
                "labels_per_item": 1,
                "score_against": None,
                "show_prior": True,
            }
        ],
        "final": ["annotate"],
    }


def test_key_order_is_the_documented_one():
    resolved = resolve(make([{"name": "a", "assign_to": "x"}]))
    assert list(resolved) == ["workflow", "dataset", "label_task", "stages", "final"]
    assert list(resolved["stages"][0]) == [
        "name",
        "input",
        "assign_to",
        "sample",
        "labels_per_item",
        "score_against",
        "show_prior",
    ]


def test_assign_to_is_always_a_list():
    config = make([{"name": "a", "assign_to": "x"}, {"name": "b", "assign_to": ["x", "y"]}])
    assert [stage["assign_to"] for stage in resolve(config)["stages"]] == [["x"], ["x", "y"]]


def test_inputs_are_spelled_out():
    config = make(
        [
            {"name": "a", "assign_to": "x", "labels_per_item": 2},
            {"name": "b", "assign_to": "x", "input": "a.disagreed"},
        ]
    )
    assert [stage["input"] for stage in resolve(config)["stages"]] == [None, "a.disagreed"]


def test_omitted_final_is_spelled_out_last_listed_first():
    config = make([{"name": "a", "assign_to": "x"}, {"name": "b", "assign_to": "x", "input": "a"}])
    assert resolve(config)["final"] == ["b", "a"]


def test_final_none_stays_none():
    assert resolve(make([{"name": "a", "assign_to": "x"}], final="none"))["final"] == "none"


def test_labels_per_item_forms():
    config = make(
        [
            {"name": "a", "assign_to": "x", "labels_per_item": "all"},
            {"name": "b", "assign_to": "x", "labels_per_item": 3},
            {"name": "c", "assign_to": "x", "labels_per_item": {"count": 2, "fraction": 0.25}},
        ],
        final="none",
    )
    assert [stage["labels_per_item"] for stage in resolve(config)["stages"]] == [
        "all",
        3,
        {"count": 2, "fraction": 0.25},
    ]


def test_resolve_gives_plain_json_types():
    config = validate_config((EXAMPLES_DIR / "training_round.yaml").read_text()).config
    resolved = resolve(config)
    assert json.loads(json.dumps(resolved)) == resolved
    assert isinstance(resolved["stages"], list)


def test_resolve_can_be_read_back_as_a_config():
    for path in EXAMPLE_FILES:
        config = validate_config(path.read_text()).config
        assert resolve(WorkflowConfig.model_validate(resolve(config))) == resolve(config)


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_resolved_yaml_snapshot(path, assert_snapshot):
    config = validate_config(path.read_text()).config
    assert_snapshot(TESTS_DIR / path.name, resolved_yaml(config))


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.stem)
def test_resolved_yaml_reads_back_to_the_same_config(path):
    config = validate_config(path.read_text()).config
    report = validate_config(resolved_yaml(config))
    assert report.ok, report.render()
    assert resolve(report.config) == resolve(config)
    assert resolved_yaml(report.config) == resolved_yaml(config)  # nothing changes on re-reading


def test_resolved_yaml_says_which_stages_take_every_item_of_the_dataset():
    config = validate_config((EXAMPLES_DIR / "review_sample.yaml").read_text()).config
    text = resolved_yaml(config)
    assert "input: null" in text
    assert "every item of the dataset 'xray_batch_1'" in text


def test_names_that_yaml_would_misread_are_quoted():
    config = make(
        [
            {"name": "no", "assign_to": ["yes", "null"]},
            {"name": "on", "assign_to": "true", "input": "no"},
        ],
        workflow="off",
        final=["on", "no"],
    )
    parsed = yaml.safe_load(resolved_yaml(config))
    assert parsed["workflow"] == "off"
    assert [stage["name"] for stage in parsed["stages"]] == ["no", "on"]
    assert parsed["stages"][0]["assign_to"] == ["yes", "null"]
    assert resolve(WorkflowConfig.model_validate(parsed)) == resolve(config)


def test_comments_describe_the_settings():
    config = make(
        [
            {
                "name": "a",
                "assign_to": ["x", "y"],
                "sample": 0.2,
                "labels_per_item": {"count": 3, "fraction": 0.2},
                "show_prior": False,
                "score_against": "gold_key",
            }
        ]
    )
    text = resolved_yaml(config)
    assert "# 20% of arriving items enter (by hashed draw)" in text
    assert "# 20% of items get 3 labels, the rest get 1" in text
    assert "# blind: labelers do not see earlier labels" in text
    assert "# each label is scored against the item property 'gold_key'" in text
    assert "# anyone holding any of these roles" in text
