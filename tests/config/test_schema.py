"""The published JSON Schema: it must match the models, and accept what the models accept."""

import json
import subprocess
import sys
from pathlib import Path

from labelflow.engine.config import config_json_schema, config_json_schema_text, validate_config

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_FILES = sorted((REPO_ROOT / "examples" / "workflows").glob("*.yaml"))
SCHEMA_FILE = REPO_ROOT / "schema" / "workflow-config.schema.json"
REGENERATE = "python -m labelflow.engine.config.schema > schema/workflow-config.schema.json"


def test_the_committed_schema_is_up_to_date():
    committed = SCHEMA_FILE.read_text() if SCHEMA_FILE.exists() else ""
    assert committed == config_json_schema_text(), (
        "schema/workflow-config.schema.json does not match the config models. "
        f"Regenerate it from the repository root with:\n    {REGENERATE}"
    )


def test_the_schema_file_is_pretty_printed_and_sorted():
    text = SCHEMA_FILE.read_text()
    assert text.endswith("}\n")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_the_module_prints_the_same_schema():
    result = subprocess.run(
        [sys.executable, "-W", "error", "-m", "labelflow.engine.config.schema"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert result.stdout == SCHEMA_FILE.read_text()
    assert result.stderr == ""


def test_the_schema_has_a_dialect_title_and_description_but_no_id():
    schema = config_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Labeling workflow config"
    assert schema["description"]
    assert "$id" not in schema


def test_assign_to_accepts_a_string_or_a_list():
    stage = config_json_schema()["$defs"]["Stage"]["properties"]["assign_to"]
    kinds = {option["type"] for option in stage["anyOf"]}
    assert kinds == {"string", "array"}


def test_the_schema_rejects_invented_keys_at_every_level():
    schema = config_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Stage"]["additionalProperties"] is False
    assert schema["$defs"]["LabelsFraction"]["additionalProperties"] is False


def test_the_schema_lists_the_whole_vocabulary():
    schema = config_json_schema()
    assert set(schema["properties"]) == {"workflow", "dataset", "label_task", "stages", "final"}
    assert set(schema["$defs"]["Stage"]["properties"]) == {
        "name",
        "input",
        "assign_to",
        "sample",
        "labels_per_item",
        "score_against",
        "show_prior",
    }
    assert schema["required"] == ["workflow", "dataset", "label_task", "stages"]


def test_every_property_has_a_description_for_assistants():
    schema = config_json_schema()
    for properties in (schema["properties"], schema["$defs"]["Stage"]["properties"]):
        for name, definition in properties.items():
            assert definition.get("description"), name


def test_the_examples_fit_the_shape_the_schema_describes():
    # No JSON Schema library is installed, so check the top-level shape by hand.
    schema = config_json_schema()
    for path in EXAMPLE_FILES:
        document = validate_config(path.read_text()).config.model_dump()
        assert set(document) <= set(schema["properties"])
        assert set(schema["required"]) <= set(document)
