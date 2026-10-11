"""Admin actions on a running workflow: exclude an item, cancel a stage's open slots."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ltq.api.deps import AdminProject
from ltq.api.errors import errors

router = APIRouter(
    prefix="/projects/{project}/workflows/{workflow}",
    tags=["admin: actions"],
    responses=errors(401, 403, 404, 422),
)


class ReasonIn(BaseModel):
    reason: str = Field(
        min_length=1, max_length=1000, description="Why. It is kept in the record of the action."
    )


class ExcludeOut(BaseModel):
    item: str = Field(description="The item that was excluded.")
    cancelled_slots: list[int] = Field(description="The slots that were cancelled with it.")


class CancelOut(BaseModel):
    stage: str = Field(description="The stage whose open slots were cancelled.")
    cancelled_slots: list[int] = Field(description="The slots that were cancelled.")


@router.post(
    "/items/{item:path}/exclude",
    response_model=ExcludeOut,
    summary="Exclude an item from a workflow",
    responses=errors(409),
)
def exclude_item(workflow: str, item: str, body: ReasonIn, ctx: AdminProject) -> ExcludeOut:
    """Take an item out of this workflow, for example an image that cannot be labeled. Its
    open and claimed slots are cancelled, and it gets no final label. An item that already
    has its final label cannot be excluded."""
    cancelled = ctx.engine.exclude_item(workflow, item, ctx.actor, body.reason)
    return ExcludeOut(item=item, cancelled_slots=cancelled)


@router.post(
    "/stages/{stage}/cancel-open-slots",
    response_model=CancelOut,
    summary="Cancel the open slots of a stage",
    responses=errors(409),
)
def cancel_open_slots(workflow: str, stage: str, body: ReasonIn, ctx: AdminProject) -> CancelOut:
    """Cancel every slot of a stage that nobody has claimed yet. Slots people are working on
    are left alone. Items whose last slot in the stage is cancelled move on."""
    cancelled = ctx.engine.cancel_open_slots(workflow, stage, ctx.actor, body.reason)
    return CancelOut(stage=stage, cancelled_slots=cancelled)
