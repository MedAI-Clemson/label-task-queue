"""Feedback: explain an item, the reports, the two exports, and the audit view of the log."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from labelflow.api import tabular
from labelflow.api.deps import AdminProject
from labelflow.api.errors import errors
from labelflow.engine import Event, Explanation, Page

router = APIRouter(
    prefix="/projects/{project}",
    tags=["admin: feedback"],
    responses=errors(401, 403, 404, 422),
)


class ExplainOut(BaseModel):
    explanation: Explanation = Field(
        description="The item's status, final label and every logged event about it, each "
        "with the rule that caused it, plus why any open slot is waiting."
    )
    text: str = Field(description="The same explanation as readable text.")


class ScoreRow(BaseModel):
    workflow: str = Field(description="The workflow.")
    stage: str = Field(description="The stage that scores labels against a key.")
    score_against: str = Field(description="The item property that holds the key.")
    labeler: str = Field(description="The person who made the labels.")
    labels: int = Field(description="Labels the person made in this stage.")
    scored: int = Field(description="Of those, how many had a valid key to compare with.")
    no_key: int = Field(description="Labels whose item had no usable key.")
    agreed: int = Field(description="Scored labels that agreed with the key.")
    fraction_agreed: float | None = Field(description="agreed / scored (empty if none scored).")


class SamplingRow(BaseModel):
    workflow: str = Field(description="The workflow.")
    stage: str = Field(description="The stage.")
    input: str | None = Field(description="Where the stage takes its items from.")
    sample: float = Field(description="The sample setting these arrivals were compared with.")
    upstream_labeler: str | None = Field(
        description="The person who labeled the item in the earlier stage; empty on the row "
        "that counts every arrival."
    )
    arrived: int = Field(description="Items that arrived at the stage.")
    admitted: int = Field(description="Of those, items that were admitted.")
    rate: float | None = Field(description="admitted / arrived: the sampling rate in fact.")
    slots: int = Field(description="Slots opened for these arrivals.")


@router.get(
    "/workflows/{workflow}/items/{item:path}/explain",
    response_model=ExplainOut,
    summary="Explain an item",
)
def explain_item(workflow: str, item: str, ctx: AdminProject) -> ExplainOut:
    """Say why an item went where it went, and why it is waiting if it is: every logged
    event about it with its cause, the draws and thresholds behind sampling decisions, and
    how its final label was decided."""
    explanation = ctx.engine.explain(workflow, item)
    return ExplainOut(explanation=explanation, text=explanation.render())


@router.get(
    "/workflows/{workflow}/reports/scores",
    response_model=list[ScoreRow],
    summary="Score report",
)
def score_report(workflow: str, ctx: AdminProject) -> list[ScoreRow]:
    """For each stage that scores against a key, how often each person's labels agreed with
    the key."""
    return [ScoreRow(**row) for row in ctx.engine.score_report(workflow)]


@router.get(
    "/workflows/{workflow}/reports/sampling",
    response_model=list[SamplingRow],
    summary="Sampling report",
)
def sampling_report(workflow: str, ctx: AdminProject) -> list[SamplingRow]:
    """For each stage, how many items arrived and how many were admitted, in all and per
    upstream labeler, so you can check that a 20% sample really was about 20%."""
    return [SamplingRow(**row) for row in ctx.engine.sampling_report(workflow)]


_EXPORT = {
    200: {
        "description": "The table, as a file.",
        "content": {tabular.CSV.split(";")[0]: {}, tabular.PARQUET: {}},
    }
}
Format = Annotated[
    Literal["csv", "parquet"], Query(description="The file format: `csv` or `parquet`.")
]


def _file(rows: list[dict[str, Any]], columns: dict[str, str], name: str, format: str) -> Response:
    if format == "csv":
        content, media = tabular.to_csv(rows, columns), tabular.CSV
    else:
        content, media = tabular.to_parquet(rows, columns), tabular.PARQUET
    headers = {"Content-Disposition": f'attachment; filename="{name}.{format}"'}
    return Response(content, media_type=media, headers=headers)


@router.get(
    "/workflows/{workflow}/exports/items",
    summary="Export the items table",
    response_class=Response,
    responses=_EXPORT,
)
def export_items(workflow: str, ctx: AdminProject, format: Format = "csv") -> Response:
    """Download one row per item: its final label, how it was decided, and its status."""
    rows = ctx.engine.export_items(workflow)
    return _file(rows, tabular.ITEM_COLUMNS, f"{workflow}_items", format)


@router.get(
    "/workflows/{workflow}/exports/labels",
    summary="Export the labels table",
    response_class=Response,
    responses=_EXPORT,
)
def export_labels(workflow: str, ctx: AdminProject, format: Format = "csv") -> Response:
    """Download one row per label: who made it, the role they held then, the stage, the
    config version, what they were shown, and the score against a key where there is one."""
    rows = ctx.engine.export_labels(workflow)
    return _file(rows, tabular.LABEL_COLUMNS, f"{workflow}_labels", format)


@router.get("/events", response_model=Page[Event], summary="Audit view of the event log")
def list_events(
    ctx: AdminProject,
    limit: Annotated[int, Query(ge=1, le=500, description="How many events to return.")] = 100,
    offset: Annotated[int, Query(ge=0, description="How many events to skip.")] = 0,
    workflow: Annotated[str | None, Query(description="Only events of this workflow.")] = None,
    item: Annotated[str | None, Query(description="Only events about this item.")] = None,
    type: Annotated[
        str | None, Query(description="Only events of this type, such as `label_submitted`.")
    ] = None,
) -> Page[Event]:
    """Read the project's event log, oldest first, one page at a time. Every change in the
    project is an event: role changes, imports, claims, labels, edits and exclusions. Each
    has its type, time, who did it, what it is about, and `because`: the rule that caused
    it."""
    return ctx.engine.events(offset, limit, workflow=workflow, item=item, type=type)
