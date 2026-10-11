"""Admin actions on a running workflow: exclude an item, cancel a stage's open slots, and
pause or resume claims in a stage or the whole workflow."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from labelflow.api import reads
from labelflow.api.deps import AdminProject
from labelflow.api.errors import errors
from labelflow.api.routes.workflows import PauseOut

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
    open and claimed slots are cancelled. An item that already has its final label may be
    excluded too: the final label and the labels stay on record, and exports show the item
    as excluded."""
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


class StagePauseOut(BaseModel):
    stage: str = Field(description="The stage's name.")
    paused: bool = Field(description="Whether the stage is paused on its own.")
    pause: PauseOut | None = Field(description="Who paused the stage, when and why, if paused.")


class PausesOut(BaseModel):
    workflow: str = Field(description="The workflow's name.")
    paused: bool = Field(description="Whether the whole workflow is paused.")
    pause: PauseOut | None = Field(description="Who paused the workflow, when and why, if paused.")
    stages: list[StagePauseOut] = Field(
        description="Each stage of the config in force, with its own pause state. A stage's "
        "slots can be claimed only when neither it nor the workflow is paused."
    )


def _pauses(ctx: AdminProject, workflow: str) -> PausesOut:
    return PausesOut(**reads.pauses(ctx.engine.store, workflow))


@router.post(
    "/pause",
    response_model=PausesOut,
    summary="Pause a whole workflow",
    responses=errors(409),
)
def pause_workflow(workflow: str, body: ReasonIn, ctx: AdminProject) -> PausesOut:
    """Stop all claims in the workflow until it is resumed. Only claims stop: new items are
    still admitted and their slots wait, claims already held can still be submitted or given
    back, and items still move on and get their final labels. Pausing a paused workflow is
    refused (409)."""
    ctx.engine.pause_workflow(workflow, ctx.actor, body.reason)
    return _pauses(ctx, workflow)


@router.post(
    "/resume",
    response_model=PausesOut,
    summary="Resume a paused workflow",
    responses=errors(409),
)
def resume_workflow(workflow: str, body: ReasonIn, ctx: AdminProject) -> PausesOut:
    """Allow claims in the workflow again. Stages that were paused on their own stay paused.
    Resuming a workflow that is not paused is refused (409)."""
    ctx.engine.resume_workflow(workflow, ctx.actor, body.reason)
    return _pauses(ctx, workflow)


@router.post(
    "/stages/{stage}/pause",
    response_model=PausesOut,
    summary="Pause a stage",
    responses=errors(409),
)
def pause_stage(workflow: str, stage: str, body: ReasonIn, ctx: AdminProject) -> PausesOut:
    """Stop claims in one stage until it is resumed; other stages go on. Only claims stop, as
    for a paused workflow, and the pause stays in force through later edits of the config.
    Pausing a paused stage is refused (409)."""
    ctx.engine.pause_stage(workflow, stage, ctx.actor, body.reason)
    return _pauses(ctx, workflow)


@router.post(
    "/stages/{stage}/resume",
    response_model=PausesOut,
    summary="Resume a paused stage",
    responses=errors(409),
)
def resume_stage(workflow: str, stage: str, body: ReasonIn, ctx: AdminProject) -> PausesOut:
    """Allow claims in a paused stage again, unless the whole workflow is paused. Resuming a
    stage that is not paused is refused (409)."""
    ctx.engine.resume_stage(workflow, stage, ctx.actor, body.reason)
    return _pauses(ctx, workflow)
