"""The ontology file: loading the example, versioning, the registry, and bad files."""

import hashlib
import json
import re
import textwrap

import pytest
import yaml

from labelflow.labeltask import (
    LabelTask,
    LabelTaskRegistry,
    OntologyError,
    OntologyLabelTask,
    load_ontology,
    registry_from_directory,
)

VIEW = """\
classifications:
  - name: view
    values: [PA, AP]
"""
MINIMAL = "name: tiny\nversion: 1\n" + VIEW


# --- The example ontology -----------------------------------------------------------------


def test_example_ontology_loads(task):
    assert isinstance(task, OntologyLabelTask)
    assert isinstance(task, LabelTask)
    assert task.name == "image_ontology"
    assert task.version == 1
    assert task.ref == "image_ontology@1"
    assert task.consensus_rule_version == "ontology-consensus@1"
    classifications = task.ontology.classifications
    assert [(c.name, c.values, c.required) for c in classifications] == [
        ("view", ("PA", "AP", "lateral"), True),
        ("quality", ("good", "poor"), False),
    ]
    assert [(m.name, m.threshold) for m in task.ontology.masks] == [
        ("lung", 0.8),
        ("effusion", 0.5),
    ]


def test_load_from_a_path_a_file_name_or_yaml_text(example_dir, tmp_path):
    path = example_dir / "chest_xray.yaml"
    from_path = load_ontology(path)
    from_text = load_ontology(path.read_text())
    from_name = load_ontology(str(path))
    assert from_path.ontology == from_text.ontology == from_name.ontology
    (tmp_path / "tiny.yaml").write_text(MINIMAL)
    assert load_ontology(tmp_path / "tiny.yaml").ref == "tiny@1"


# --- Ontology version ---------------------------------------------------------------------


def test_ontology_version_is_name_version_and_a_hash_of_the_canonical_json(task, example_dir):
    data = yaml.safe_load((example_dir / "chest_xray.yaml").read_text())
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    assert task.ontology_version == f"image_ontology@1+{digest[:12]}"
    assert re.fullmatch(r"image_ontology@1\+[0-9a-f]{12}", task.ontology_version)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param("[PA, AP]", "[PA, AP, lateral]", id="a-value-added"),
        pytest.param("[PA, AP]", "[AP, PA]", id="values-reordered"),
        pytest.param(
            "version: 1\n", "version: 1\nmasks: [{name: lung, threshold: 0.8}]\n", id="a-mask-added"
        ),
    ],
)
def test_an_edit_without_a_version_bump_is_detectable(before, after):
    original = load_ontology(MINIMAL)
    edited = load_ontology(MINIMAL.replace(before, after))
    assert edited.ref == original.ref
    assert edited.ontology_version != original.ontology_version
    assert edited.ontology_version.startswith("tiny@1+")


def test_changing_a_threshold_changes_the_version(example_dir):
    text = (example_dir / "chest_xray.yaml").read_text()
    edited = load_ontology(text.replace("threshold: 0.8", "threshold: 0.75"))
    assert edited.ontology_version != load_ontology(text).ontology_version


def test_the_mask_pixel_cap_is_part_of_the_version_only_when_set():
    task = load_ontology(MINIMAL)
    assert task.ontology.max_mask_pixels is None
    assert task.ontology.mask_pixel_cap == 64 * 1024 * 1024
    capped = load_ontology(MINIMAL + "max_mask_pixels: 1000000\n")
    assert capped.ontology.max_mask_pixels == capped.ontology.mask_pixel_cap == 1_000_000
    assert capped.ontology_version != task.ontology_version
    # the key left out does not enter the fingerprint, so fingerprints from before it existed hold
    data = task.ontology.model_dump(mode="json")
    assert data.pop("max_mask_pixels") is None
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    assert task.ontology_version.endswith(hashlib.sha256(canonical.encode()).hexdigest()[:12])


def test_layout_and_comments_do_not_change_the_version():
    reordered = """\
    # a comment, and the keys in another order
    classifications:
      - values: [PA, AP]
        required: true      # the default, written out
        name: view
    version: 1
    name: tiny
    """
    assert (
        load_ontology(textwrap.dedent(reordered)).ontology_version
        == load_ontology(MINIMAL).ontology_version
    )


# --- The registry -------------------------------------------------------------------------


def test_registry_from_the_example_directory(example_dir):
    registry = registry_from_directory(example_dir)
    assert isinstance(registry, LabelTaskRegistry)
    assert "image_ontology@1" in registry
    assert registry.get("image_ontology@1").ref == "image_ontology@1"


def test_registry_loads_every_yaml_file_and_nothing_else(tmp_path):
    (tmp_path / "a.yaml").write_text(MINIMAL)
    (tmp_path / "b.yaml").write_text(
        MINIMAL.replace("tiny", "other").replace("version: 1", "version: 3")
    )
    (tmp_path / "notes.txt").write_text("not an ontology")
    (tmp_path / "old.yml").write_text("not read: only *.yaml files are")
    (tmp_path / "folder.yaml").mkdir()
    registry = registry_from_directory(tmp_path)
    assert registry.refs() == ["other@3", "tiny@1"]


def test_an_empty_directory_gives_an_empty_registry(tmp_path):
    assert registry_from_directory(tmp_path).refs() == []


def test_registry_reports_a_bad_file_by_name(tmp_path):
    (tmp_path / "good.yaml").write_text(MINIMAL)
    (tmp_path / "bad.yaml").write_text(
        "name: other\nversion: 0\nmasks: [{name: m, threshold: 2}]\n"
    )
    with pytest.raises(OntologyError) as info:
        registry_from_directory(tmp_path)
    assert info.value.problems == [
        "bad.yaml: Version must be a whole number from 1 up, but it is 0",
        "bad.yaml: mask class 'm': threshold must be above 0 and at most 1 (it is the IoU at "
        "or above which two masks agree), but it is 2.0",
    ]


def test_registry_reports_all_bad_files_together(tmp_path):
    (tmp_path / "one.yaml").write_text("name: one\n")
    (tmp_path / "two.yaml").write_text("")
    with pytest.raises(OntologyError) as info:
        registry_from_directory(tmp_path)
    assert any(problem.startswith("one.yaml: ") for problem in info.value.problems)
    assert "two.yaml: The file is empty." in info.value.problems


def test_registry_refuses_two_files_with_the_same_name_and_version(tmp_path):
    (tmp_path / "a.yaml").write_text(MINIMAL)
    (tmp_path / "b.yaml").write_text(MINIMAL)
    with pytest.raises(OntologyError) as info:
        registry_from_directory(tmp_path)
    assert info.value.problems == [
        "b.yaml: 'tiny@1' is already defined in a.yaml. Each ontology file needs its own "
        "name and version."
    ]


def test_registry_needs_a_directory_that_exists(tmp_path):
    with pytest.raises(OntologyError, match="There is no folder at"):
        registry_from_directory(tmp_path / "missing")


# --- Bad ontology files -------------------------------------------------------------------


def problems_in(text: str) -> list[str]:
    with pytest.raises(OntologyError) as info:
        load_ontology(text if text.endswith("\n") else text + "\n")
    return info.value.problems


BAD_FILES = [
    pytest.param("# nothing here\n", ["The file is empty."], id="empty"),
    pytest.param(
        "- name: view\n",
        [
            "The file must be a set of keys and values (name, version, classifications, masks), "
            "but it is a list."
        ],
        id="a-list",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nname: other\n",
        [
            "The file could not be read as YAML (line 3, column 1): the key 'name' appears more "
            "than once."
        ],
        id="repeated-key",
    ),
    pytest.param(
        "name: [unclosed\n",
        ["The file could not be read as YAML (line 2, column 1):"],
        id="not-yaml",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclasifications: []\n",
        [
            "Unknown key 'clasifications'. Did you mean 'classifications'? The allowed keys are "
            "'name', 'version', 'classifications', 'masks', 'max_mask_pixels'."
        ],
        id="misspelled-top-level-key",
    ),
    pytest.param(
        "version: 1\n" + VIEW,
        ["'name' is required but missing."],
        id="no-name",
    ),
    pytest.param(
        "name: 'my ontology'\nversion: 1\n" + VIEW,
        [
            "Name 'my ontology' must start with a letter and use only letters, digits and "
            "underscores, so that a workflow can refer to it as name@version, for example "
            "image_ontology@1"
        ],
        id="name-with-a-space",
    ),
    pytest.param(
        "name: tiny\nversion: '1'\n" + VIEW,
        ["'version' must be a whole number."],
        id="version-in-quotes",
    ),
    pytest.param(
        "name: tiny\nversion: 0\n" + VIEW,
        ["Version must be a whole number from 1 up, but it is 0"],
        id="version-zero",
    ),
    pytest.param(
        "name: tiny\nversion: 1\n",
        ["The ontology has no classifications and no masks, so there is nothing to label."],
        id="nothing-to-label",
    ),
    pytest.param(
        MINIMAL + "max_mask_pixels: 0\n",
        [
            "Max_mask_pixels must be a whole number from 1 to 268435456 (16384 x 16384, the "
            "largest mask the interface can show), but it is 0. Leave it out for the default "
            "of 67108864 (64 megapixels)"
        ],
        id="pixel-cap-zero",
    ),
    pytest.param(
        MINIMAL + "max_mask_pixels: 300000000\n",
        ["Max_mask_pixels must be a whole number from 1 to 268435456"],
        id="pixel-cap-above-the-largest-mask",
    ),
    pytest.param(
        MINIMAL + "max_mask_pixels: 1e6\n",
        ["'max_mask_pixels' must be a whole number."],
        id="pixel-cap-not-a-whole-number",
    ),
    pytest.param(
        MINIMAL + "max_mask_pixels: '1000000'\n",
        ["'max_mask_pixels' must be a whole number."],
        id="pixel-cap-in-quotes",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: []\n",
        ["classification 'view': values must list at least one allowed value"],
        id="no-values",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: [PA, AP, PA]\n",
        ["classification 'view': values lists 'PA' more than once"],
        id="repeated-value",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: [PA, '']\n",
        ["classification 'view': values must not contain an empty or blank value"],
        id="blank-value",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: ' '\n    values: [PA]\n",
        ["classification ' ': name must not be empty"],
        id="blank-name",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: smoker\n    values: [yes, no]\n",
        [
            "classification 'smoker': item 1 of 'values' must be text. In YAML, put quotes around "
            "words such as yes, no, on and off, and around numbers, if you mean them as text.",
            "classification 'smoker': item 2 of 'values' must be text.",
        ],
        id="yes-and-no-are-booleans-in-yaml",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n",
        ["classification 'view': 'values' is required but missing."],
        id="classification-without-values",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: [PA]\n"
        "    required: sometimes\n",
        ["classification 'view': 'required' must be true or false."],
        id="required-is-not-a-boolean",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: [PA]\n"
        "    colour: red\n",
        [
            "classification 'view': unknown key 'colour'. The allowed keys are 'name', 'values', "
            "'required'."
        ],
        id="unknown-classification-key",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - values: [PA]\n",
        ["classification #1: 'name' is required but missing."],
        id="classification-without-a-name",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - view\n",
        ["Item 1 of 'classifications' must be a set of keys and values (such as name: lung)."],
        id="classification-is-not-a-mapping",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications: view\n",
        ["'classifications' must be a list."],
        id="classifications-is-not-a-list",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n",
        ["mask class 'lung': 'threshold' is required but missing."],
        id="mask-without-threshold",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n    treshold: 0.8\n",
        [
            "mask class 'lung': unknown key 'treshold'. Did you mean 'threshold'? The allowed keys "
            "are 'name', 'threshold'.",
            "mask class 'lung': 'threshold' is required but missing.",
        ],
        id="misspelled-threshold",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n    threshold: 0\n",
        [
            "mask class 'lung': threshold must be above 0 and at most 1 (it is the IoU at or above "
            "which two masks agree), but it is 0.0"
        ],
        id="threshold-zero",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n    threshold: 1.5\n",
        [
            "mask class 'lung': threshold must be above 0 and at most 1 (it is the IoU at or above "
            "which two masks agree), but it is 1.5"
        ],
        id="threshold-above-one",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n    threshold: high\n",
        ["mask class 'lung': 'threshold' must be a number."],
        id="threshold-is-text",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nmasks:\n  - name: lung\n    threshold: .nan\n",
        ["mask class 'lung': threshold must be above 0 and at most 1"],
        id="threshold-is-not-a-number",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: lung\n    values: [a]\n"
        "masks:\n  - name: lung\n    threshold: 0.5\n",
        [
            "Names must be unique across classifications and masks, but 'lung' appears more than "
            "once."
        ],
        id="name-used-twice-across-kinds",
    ),
    pytest.param(
        "name: tiny\nversion: 1\nclassifications:\n  - name: view\n    values: [a]\n"
        "  - name: view\n    values: [b]\n",
        [
            "Names must be unique across classifications and masks, but 'view' appears more than "
            "once."
        ],
        id="name-used-twice",
    ),
]


@pytest.mark.parametrize(("text", "expected"), BAD_FILES)
def test_bad_ontology_files_get_plain_language_errors(text, expected):
    found = problems_in(text)
    for message in expected:
        assert any(problem.startswith(message) for problem in found), found


def test_one_error_lists_every_problem():
    text = """\
    name: tiny
    version: 0
    classifications:
      - name: view
        values: []
      - name: quality
    masks:
      - name: lung
        threshold: 1.5
    """
    found = problems_in(textwrap.dedent(text))
    assert len(found) == 4
    with pytest.raises(OntologyError) as info:
        load_ontology(textwrap.dedent(text))
    message = str(info.value)
    assert message.startswith("The ontology (given as text) has 4 problems:\n")
    assert all(f"  - {problem}" in message for problem in found)


def test_the_error_names_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: tiny\nversion: 0\nclassifications:\n  - name: v\n    values: [a]\n")
    with pytest.raises(OntologyError) as info:
        load_ontology(path)
    assert str(info.value).startswith("The ontology (file 'broken.yaml') has 1 problem:\n")
    assert info.value.source == "file 'broken.yaml'"


def test_a_missing_file_is_reported_plainly(tmp_path):
    with pytest.raises(OntologyError, match="There is no file at"):
        load_ontology(tmp_path / "missing.yaml")
    with pytest.raises(OntologyError, match="There is no file at"):
        load_ontology(str(tmp_path / "missing.yaml"))


def test_an_ontology_error_is_a_value_error():
    with pytest.raises(ValueError):
        load_ontology("name: x\n")
