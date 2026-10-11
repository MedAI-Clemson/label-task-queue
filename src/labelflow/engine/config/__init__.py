"""The labeling workflow config: models, validation, the resolved view, diagram and schema.

How YAML text becomes a ``WorkflowConfig``, and where the problems along the way are
worded (every message is for the researcher, never Pydantic's own):

- ``validate.py`` reads the text (``validate_config``) and puts the parts together.
- ``models.py`` holds the config itself; any ``WorkflowConfig`` that exists is valid.
- ``translate.py`` turns Pydantic's field errors into plain-language problems.
- ``graph.py`` checks how the stages fit together, on the model and on a raw document.
- ``problems.py`` is the shared vocabulary: ``Problem``, locations, and wording helpers.

``resolve.py``, ``diagram.py`` and ``json_schema.py`` are read-only views of a config.
"""

from labelflow.engine.config.diagram import to_mermaid
from labelflow.engine.config.graph import ConfigGraphError
from labelflow.engine.config.json_schema import config_json_schema, config_json_schema_text
from labelflow.engine.config.models import (
    InputRef,
    LabelsFraction,
    LabelsPerItem,
    Stage,
    WorkflowConfig,
)
from labelflow.engine.config.problems import (
    Problem,
    and_list,
    did_you_mean,
    percent,
    plural,
    quoted_list,
)
from labelflow.engine.config.resolve import resolve, resolved_yaml
from labelflow.engine.config.validate import (
    ConfigReport,
    ValidationContext,
    role_warnings,
    validate_config,
)

__all__ = [
    "ConfigGraphError",
    "ConfigReport",
    "InputRef",
    "LabelsFraction",
    "LabelsPerItem",
    "Problem",
    "Stage",
    "ValidationContext",
    "WorkflowConfig",
    "and_list",
    "config_json_schema",
    "config_json_schema_text",
    "did_you_mean",
    "percent",
    "plural",
    "quoted_list",
    "resolve",
    "resolved_yaml",
    "role_warnings",
    "to_mermaid",
    "validate_config",
]
