"""Property tests (Hypothesis) over generated workflows.

1. The resolved view can be read back: parsing ``resolved_yaml(c)``, or validating
   ``resolve(c)``, gives a config that resolves identically to ``c``.
2. ``validate_config`` never raises, and agrees with ``WorkflowConfig`` about whether a
   document is valid, even for documents that are broken in odd ways.
"""

import copy
import json
import string

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from labelflow.engine.config import (
    WorkflowConfig,
    config_json_schema,
    resolve,
    resolved_yaml,
    to_mermaid,
    validate_config,
)

PROPERTY_SETTINGS = settings(max_examples=150, deadline=None, database=None)

# Words that YAML would read as booleans or null, mixed in to test quoting.
AWKWARD_NAMES = ["yes", "no", "null", "true", "false", "on", "off", "none", "Y", "n", "a"]
NAME_CHARACTERS = string.ascii_letters + string.digits + "_"

names = st.one_of(
    st.sampled_from(AWKWARD_NAMES),
    st.builds(
        lambda first, rest: first + rest,
        st.sampled_from(string.ascii_letters),
        st.text(NAME_CHARACTERS, max_size=8),
    ),
)
roles = st.one_of(names, st.lists(names, min_size=1, max_size=3, unique=True))
samples = st.one_of(st.just(1), st.floats(min_value=0.001, max_value=1.0))
labels = st.one_of(
    st.integers(min_value=1, max_value=4),
    st.just("all"),
    st.fixed_dictionaries(
        {"count": st.integers(min_value=2, max_value=5), "fraction": st.floats(0.01, 0.99)}
    ),
)


@st.composite
def finals(draw, stage_names):
    """No `final` key, `none`, every stage in some order, or some stages in some order."""
    kind = draw(st.sampled_from(["omit", "none", "all", "some"]))
    if kind == "omit":
        return None
    if kind == "none":
        return "none"
    shuffled = draw(st.permutations(stage_names))
    return shuffled if kind == "all" else shuffled[: draw(st.integers(1, len(shuffled)))]


@st.composite
def stage_lists(draw):
    """Stages whose inputs always name an earlier stage, and use .agreed only where it can work."""
    stage_names = draw(st.lists(names, min_size=1, max_size=5, unique=True))
    stages = []
    for index, name in enumerate(stage_names):
        stage = {"name": name, "assign_to": draw(roles)}
        if index and draw(st.booleans()):
            upstream = draw(st.sampled_from(stages))
            can_branch = upstream.get("labels_per_item", 1) != 1
            suffix = draw(st.sampled_from(["", ".agreed", ".disagreed"] if can_branch else [""]))
            stage["input"] = upstream["name"] + suffix
        if draw(st.booleans()):
            stage["sample"] = draw(samples)
        if draw(st.booleans()):
            stage["labels_per_item"] = draw(labels)
        if draw(st.booleans()):
            stage["score_against"] = draw(names)
        if draw(st.booleans()):
            stage["show_prior"] = draw(st.booleans())
        stages.append(stage)
    return stages


def is_valid(document):
    try:
        WorkflowConfig.model_validate(document)
    except ValidationError:
        return False
    return True


@st.composite
def valid_documents(draw):
    stages = draw(stage_lists())
    document = {
        "workflow": draw(names),
        "dataset": draw(names),
        "label_task": draw(names) + "@" + str(draw(st.integers(0, 20))),
        "stages": stages,
    }
    final = draw(finals([stage["name"] for stage in stages]))
    if final is not None:
        document["final"] = final
    return document


valid_configs = valid_documents().filter(is_valid)

# ---------------------------------------------------------------------------- resolved view


@PROPERTY_SETTINGS
@given(valid_configs)
def test_resolved_yaml_reads_back_to_the_same_config(document):
    config = WorkflowConfig.model_validate(document)
    report = validate_config(resolved_yaml(config))
    assert report.ok, report.render()
    assert resolve(report.config) == resolve(config)
    assert resolved_yaml(report.config) == resolved_yaml(config)


@PROPERTY_SETTINGS
@given(valid_configs)
def test_resolve_output_validates_to_a_config_that_resolves_the_same(document):
    config = WorkflowConfig.model_validate(document)
    assert resolve(WorkflowConfig.model_validate(resolve(config))) == resolve(config)
    assert json.loads(json.dumps(resolve(config))) == resolve(config)


@PROPERTY_SETTINGS
@given(valid_configs)
def test_resolved_yaml_is_plain_yaml_of_the_resolved_dict(document):
    config = WorkflowConfig.model_validate(document)
    assert yaml.safe_load(resolved_yaml(config)) == resolve(config)


@PROPERTY_SETTINGS
@given(valid_configs)
def test_the_diagram_mentions_every_stage_and_never_raises(document):
    config = WorkflowConfig.model_validate(document)
    text = to_mermaid(config)
    assert text.startswith("flowchart TD\n")
    for index, stage in enumerate(config.stages, start=1):
        assert f"    stage{index}[" in text
        assert stage.name in text


@PROPERTY_SETTINGS
@given(valid_configs)
def test_generated_configs_have_the_keys_the_schema_describes(document):
    schema = config_json_schema()
    assert set(document) <= set(schema["properties"])
    for stage in document["stages"]:
        assert set(stage) <= set(schema["$defs"]["Stage"]["properties"])


# ------------------------------------------------------------------ validate_config is safe

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-3, max_value=300)
    | st.floats()
    | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=6,
)
KEYS = [
    "workflow",
    "dataset",
    "label_task",
    "stages",
    "final",
    "name",
    "input",
    "assign_to",
    "sample",
    "labels_per_item",
    "score_against",
    "show_prior",
    "bogus",
]


@st.composite
def messy_documents(draw):
    """A valid document with up to four keys removed or replaced by random values."""
    document = copy.deepcopy(draw(valid_documents()))
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        container = document
        if draw(st.booleans()):
            stages = document.get("stages")
            stages = (
                [stage for stage in stages if isinstance(stage, dict)]
                if isinstance(stages, list)
                else []
            )
            if stages:
                container = draw(st.sampled_from(stages))
        key = draw(st.sampled_from(KEYS))
        if draw(st.booleans()):
            container.pop(key, None)
        else:
            container[key] = draw(json_values)
    return document


JARGON = ["pydantic", "validation error", "input should", "[type=", "value_error", "ctx"]


@PROPERTY_SETTINGS
@given(messy_documents())
def test_validate_config_agrees_with_the_model(document):
    report = validate_config(document)
    valid = is_valid(document)
    assert report.ok == valid
    if valid:
        assert report.config == WorkflowConfig.model_validate(document)
        assert report.errors == []
    else:
        assert report.config is None
        assert report.errors
        text = report.render().lower()
        assert not any(word in text for word in JARGON)


@PROPERTY_SETTINGS
@given(messy_documents())
def test_validate_config_gives_the_same_answer_for_yaml_text(document):
    from_dict = validate_config(document)
    from_text = validate_config(yaml.safe_dump(document))
    assert from_text.ok == from_dict.ok
    # YAML text is dumped with its keys sorted, so unknown keys can be reported in another order.
    assert sorted(str(p) for p in from_text.errors) == sorted(str(p) for p in from_dict.errors)


@PROPERTY_SETTINGS
@given(st.one_of(st.text(max_size=200), st.binary(max_size=50), json_values))
def test_validate_config_never_raises(source):
    report = validate_config(source)
    assert report.ok == (report.config is not None)
    assert report.render().endswith("\n")
