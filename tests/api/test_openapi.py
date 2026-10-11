"""The OpenAPI document: every endpoint explained in plain words."""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest
from pydantic import BaseModel

import labelflow.api.routes
from labelflow.api.app import TAGS

METHODS = ("get", "post", "put", "patch", "delete")

ENDPOINTS = {
    ("GET", "/health"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/me"),
    ("GET", "/accounts"),
    ("POST", "/accounts"),
    ("POST", "/accounts/{username}/deactivate"),
    ("POST", "/accounts/{username}/password"),
    ("GET", "/projects"),
    ("POST", "/projects"),
    ("GET", "/projects/{project}/roles"),
    ("POST", "/projects/{project}/roles"),
    ("POST", "/projects/{project}/roles/{role}/grant"),
    ("POST", "/projects/{project}/roles/{role}/revoke"),
    ("GET", "/projects/{project}/datasets"),
    ("POST", "/projects/{project}/datasets"),
    ("POST", "/projects/{project}/datasets/{dataset}/items"),
    ("GET", "/projects/{project}/datasets/{dataset}/items"),
    ("POST", "/projects/{project}/workflows/validate"),
    ("POST", "/projects/{project}/workflows"),
    ("GET", "/projects/{project}/workflows"),
    ("GET", "/projects/{project}/workflows/{workflow}"),
    ("GET", "/projects/{project}/workflows/{workflow}/versions"),
    ("POST", "/projects/{project}/workflows/{workflow}/preview-edit"),
    ("POST", "/projects/{project}/workflows/{workflow}/edits"),
    ("POST", "/projects/{project}/workflows/{workflow}/next"),
    ("POST", "/projects/{project}/workflows/{workflow}/slots/{slot}/claim"),
    ("GET", "/projects/{project}/workflows/{workflow}/slots/{slot}"),
    ("POST", "/projects/{project}/workflows/{workflow}/slots/{slot}/submit"),
    ("POST", "/projects/{project}/workflows/{workflow}/slots/{slot}/release"),
    ("POST", "/projects/{project}/workflows/{workflow}/items/{item}/exclude"),
    ("POST", "/projects/{project}/workflows/{workflow}/stages/{stage}/cancel-open-slots"),
    ("GET", "/projects/{project}/workflows/{workflow}/items/{item}/explain"),
    ("GET", "/projects/{project}/workflows/{workflow}/reports/scores"),
    ("GET", "/projects/{project}/workflows/{workflow}/reports/sampling"),
    ("GET", "/projects/{project}/workflows/{workflow}/exports/items"),
    ("GET", "/projects/{project}/workflows/{workflow}/exports/labels"),
    ("GET", "/projects/{project}/events"),
}

# Words a researcher has no use for (the design notes never use them).
JARGON = ("reducer", "idempotent", "payload", "serializ", "pydantic", "sqlalchemy", "uuid")


@pytest.fixture
def spec(app):
    return app.openapi()


def operations(spec):
    for path, item in spec["paths"].items():
        for method in METHODS:
            if method in item:
                yield method.upper(), path, item[method]


def test_the_endpoints_are_the_expected_ones(spec):
    assert {(method, path) for method, path, _ in operations(spec)} == ENDPOINTS


def test_every_endpoint_has_a_summary_and_a_short_description(spec):
    for method, path, operation in operations(spec):
        summary, description = operation.get("summary", ""), operation.get("description", "")
        assert summary, (method, path)
        assert len(summary) <= 50, (method, path, summary)
        assert description, (method, path)
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", description.strip()) if s]
        assert 1 <= len(sentences) <= 5, (method, path, len(sentences))
        assert len(description) <= 600, (method, path)


def test_the_wording_has_no_jargon(spec):
    text = " ".join(
        f"{op.get('summary', '')} {op.get('description', '')}" for _, _, op in operations(spec)
    ).lower()
    for word in JARGON:
        assert word not in text, word


def test_every_endpoint_is_tagged_by_audience(spec):
    declared = {tag["name"] for tag in TAGS}
    assert all(tag["description"] for tag in TAGS)
    for method, path, operation in operations(spec):
        assert operation["tags"], (method, path)
        assert set(operation["tags"]) <= declared, (method, path)
        if path.startswith("/accounts"):
            assert operation["tags"] == ["admin: accounts"]
    roles = [op["tags"] for m, p, op in operations(spec) if "/roles" in p]
    assert roles and all(tags == ["admin: roles"] for tags in roles)
    labeler = [p for m, p, op in operations(spec) if "labeler" in op["tags"]]
    assert any(p.endswith("/next") for p in labeler)
    assert any(p.endswith("/submit") for p in labeler)


def test_operation_ids_are_unique_and_readable(spec):
    ids = [op["operationId"] for _, _, op in operations(spec)]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z_]+", operation_id) for operation_id in ids)


def api_models():
    """Every request and response model the API defines (not the engine's own)."""
    found = {}
    modules = [importlib.import_module("labelflow.api.errors")]
    for info in pkgutil.iter_modules(labelflow.api.routes.__path__):
        modules.append(importlib.import_module(f"labelflow.api.routes.{info.name}"))
    for module in modules:
        for name, value in vars(module).items():
            is_model = isinstance(value, type) and issubclass(value, BaseModel)
            if is_model and value.__module__ == module.__name__:
                found[f"{module.__name__}.{name}"] = value
    return found


def test_every_field_of_every_api_model_is_described():
    models = api_models()
    assert len(models) > 40
    for name, model in models.items():
        for field, info in model.model_fields.items():
            assert info.description, f"{name}.{field} has no description"
            assert len(info.description) >= 8, f"{name}.{field}"


def test_logging_in_is_part_of_the_documented_security(spec):
    schemes = spec["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schemes["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "labelflow_session",
        "description": "The login cookie set by `POST /auth/login` (used by the web pages).",
    }
    me = spec["paths"]["/auth/me"]["get"]
    assert {"HTTPBearer": []} in me["security"] and {"APIKeyCookie": []} in me["security"]
    assert "security" not in spec["paths"]["/health"]["get"]
    assert "security" not in spec["paths"]["/auth/login"]["post"]


def test_the_errors_are_documented_in_their_one_shape(spec):
    def schema_of(path, method, status):
        response = spec["paths"][path][method]["responses"][status]
        return response["content"]["application/json"]["schema"]["$ref"]

    for status in ("401", "403", "422"):
        assert schema_of("/accounts", "get" if status != "422" else "post", status).endswith(
            "ErrorOut"
        )
    flow = "/projects/{project}/workflows/{workflow}"
    assert schema_of(f"{flow}/edits", "post", "409").endswith("ErrorOut")
    assert schema_of(f"{flow}/next", "post", "404").endswith("ErrorOut")
    assert "HTTPValidationError" not in str(spec["paths"])


def test_the_documented_downloads(spec):
    exports = spec["paths"]["/projects/{project}/workflows/{workflow}/exports/items"]["get"]
    content = exports["responses"]["200"]["content"]
    assert set(content) == {"text/csv", "application/vnd.apache.parquet"}
    assert (
        "204" in spec["paths"]["/projects/{project}/workflows/{workflow}/next"]["post"]["responses"]
    )


def test_the_description_explains_the_words(spec):
    description = spec["info"]["description"]
    for word in ("project", "workflow", "slot", "claims", "event log"):
        assert word in description
    assert spec["info"]["title"] == "label-task-queue"
