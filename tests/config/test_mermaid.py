"""The Mermaid diagram: snapshot tests for the examples, and checks on its details."""

from pathlib import Path

import pytest

from labelflow.engine.config import WorkflowConfig, to_mermaid, validate_config
from labelflow.engine.config.diagram import escape, ordinal

TESTS_DIR = Path(__file__).parent / "mermaid"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "workflows"
SOURCES = sorted(EXAMPLES_DIR.glob("*.yaml")) + sorted(TESTS_DIR.glob("*.yaml"))


def make(stages, final=None, dataset="d"):
    document = {"workflow": "w", "dataset": dataset, "label_task": "t@1", "stages": stages}
    if final is not None:
        document["final"] = final
    return WorkflowConfig.model_validate(document)


@pytest.mark.parametrize("source", SOURCES, ids=lambda path: path.stem)
def test_diagram_snapshot(source, assert_snapshot):
    config = validate_config(source.read_text()).config
    assert config is not None
    assert_snapshot(TESTS_DIR / f"{source.stem}.mmd", to_mermaid(config))


def test_edges_are_labeled_with_the_outcome_and_the_sample():
    config = make(
        [
            {"name": "a", "assign_to": "x", "labels_per_item": 2},
            {"name": "b", "input": "a.agreed", "assign_to": "x", "sample": 0.1},
            {"name": "c", "input": "a.disagreed", "assign_to": "x"},
            {"name": "d", "input": "c", "assign_to": "x"},
        ]
    )
    text = to_mermaid(config)
    assert 'stage1 -->|"agreed, 10% sample"| stage2' in text
    assert 'stage1 -->|"disagreed"| stage3' in text
    assert "stage3 --> stage4" in text  # a full-output edge at 100% has no label


def test_a_sampled_root_stage_labels_the_edge_from_the_dataset():
    text = to_mermaid(make([{"name": "a", "assign_to": "x", "sample": 0.5}]))
    assert 'dataset -->|"50% sample"| stage1' in text


def test_stage_boxes_show_roles_labels_blind_and_scoring():
    config = make(
        [
            {
                "name": "gold_read",
                "assign_to": ["trainee", "intern"],
                "labels_per_item": "all",
                "show_prior": False,
                "score_against": "gold_key",
            },
            {"name": "plain", "assign_to": "x", "labels_per_item": 3, "input": "gold_read"},
            {
                "name": "mixed",
                "assign_to": "x",
                "labels_per_item": {"count": 3, "fraction": 0.2},
                "input": "plain",
            },
        ]
    )
    lines = to_mermaid(config).splitlines()
    assert (
        '    stage1["gold_read<br/>roles: trainee, intern<br/>every role holder labels each '
        'item<br/>blind<br/>scored against gold_key"]'
    ) in lines
    assert '    stage2["plain<br/>role: x<br/>3 labels per item"]' in lines
    assert '    stage3["mixed<br/>role: x<br/>3 labels for 20% of items, else 1"]' in lines


def test_the_final_step_is_fed_by_the_final_stages_in_order():
    config = make(
        [
            {"name": "a", "assign_to": "x"},
            {"name": "b", "input": "a", "assign_to": "x"},
            {"name": "c", "input": "b", "assign_to": "x"},
        ],
        final=["c", "a", "b"],
    )
    text = to_mermaid(config)
    assert 'stage3 -->|"1st"| final' in text
    assert 'stage1 -->|"2nd"| final' in text
    assert 'stage2 -->|"3rd"| final' in text


def test_final_none_shows_a_note_instead_of_an_end_node():
    text = to_mermaid(make([{"name": "a", "assign_to": "x"}], final="none"))
    assert '    final["no final labels"]' in text
    assert "--> final" not in text
    assert "class final note" in text


def test_stage_names_that_are_mermaid_keywords_are_safe():
    config = make(
        [
            {"name": "end", "assign_to": "x"},
            {"name": "graph", "input": "end", "assign_to": "x"},
            {"name": "subgraph", "input": "graph", "assign_to": "x"},
        ]
    )
    text = to_mermaid(config)
    assert '    stage1["end<br/>' in text
    assert not any(
        line.strip().split(" ")[0] in {"end", "graph", "subgraph"} for line in text.splitlines()
    )


def test_text_is_escaped_for_mermaid():
    assert escape('a"b<c>d&e|f#g`h\ni') == "a#quot;b#lt;c#gt;d#38;e#124;f#35;g#96;h i"
    assert escape("plain_name") == "plain_name"


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (23, "23rd"),
        (101, "101st"),
        (111, "111th"),
    ],
)
def test_ordinals(number, text):
    assert ordinal(number) == text
