"""The JSON Schema of the config, for editors and AI assistants.

A schema lets an assistant write valid configs and lets a validator catch invented keys.
The schema is committed as ``schema/workflow-config.schema.json``; a test fails when the
committed file no longer matches the models. To regenerate it::

    python -m ltq.engine.config.schema > schema/workflow-config.schema.json
"""

from __future__ import annotations

import json
from typing import Any

from ltq.engine.config.models import WorkflowConfig

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TITLE = "Labeling workflow config"
SCHEMA_DESCRIPTION = (
    "A labeling workflow: the dataset it labels, the label task, the stages in order "
    "(each naming its input, its roles and its settings), and the final step that says where "
    "final labels come from. Stage names must be unique, an input must name an earlier stage, "
    "and every item must be able to reach a final label."
)


def config_json_schema() -> dict[str, Any]:
    """The JSON Schema for a workflow config, generated from the models."""
    schema = WorkflowConfig.model_json_schema()
    schema.update(title=SCHEMA_TITLE, description=SCHEMA_DESCRIPTION)
    return {"$schema": SCHEMA_DIALECT, **schema}


def config_json_schema_text() -> str:
    """The schema as the text stored in ``schema/workflow-config.schema.json``."""
    return json.dumps(config_json_schema(), indent=2, sort_keys=True) + "\n"
