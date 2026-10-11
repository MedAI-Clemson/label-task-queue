from fastapi.testclient import TestClient

from ltq.api import app
from ltq.engine.config import WorkflowConfig


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_example_config_parses():
    config = WorkflowConfig.model_validate(
        {
            "workflow": "xray_main",
            "dataset": "xray_batch_1",
            "label_task": "image_ontology@1",
            "stages": [
                {"name": "annotate", "assign_to": "annotator"},
                {
                    "name": "expert_review",
                    "input": "annotate",
                    "assign_to": "expert",
                    "sample": 0.2,
                },
            ],
            "final": ["expert_review", "annotate"],
        }
    )
    assert config.stage("expert_review").input_ref.stage == "annotate"
    assert config.resolved_final() == ("expert_review", "annotate")
    assert [s.name for s in config.downstream("annotate", "all")] == ["expert_review"]
