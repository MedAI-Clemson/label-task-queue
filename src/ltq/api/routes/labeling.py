"""Labeling: ask for work, look at it, submit a label, or give the work back.

The person is always the one logged in, never a user id in the address. A slot is one label
an item needs in one stage; claiming a slot reserves it for you until you submit or release.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, JsonValue

from ltq.api.deps import UserProject
from ltq.api.errors import errors
from ltq.engine import Shown, Slot, WorkPacket

router = APIRouter(
    prefix="/projects/{project}/workflows/{workflow}",
    tags=["labeler"],
    responses=errors(401, 404, 422),
)


class Work(BaseModel):
    slot: Slot = Field(description="The slot you hold.")
    packet: WorkPacket = Field(
        description="What to show: the image's address and hash, and, unless the stage is "
        "blind, the item's earlier labels with the agree/disagree flags."
    )
    shown: Shown = Field(
        description="The references to send back with the label if you showed everything in "
        "the packet. Change them if you showed less."
    )


class SubmitIn(BaseModel):
    label: dict[str, JsonValue] = Field(
        description="The label, in the shape the workflow's label task asks for."
    )
    shown: Shown = Field(
        description="What you showed the labeler, as references (see the `shown` of the work)."
    )
    duration_ms: int | None = Field(
        default=None, ge=0, description="Milliseconds from claim to submit, as you measured it."
    )


class SubmitOut(BaseModel):
    label_id: int = Field(description="The id of the new label.")


class ReleaseIn(BaseModel):
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="Why you are giving the slot back, such as an image that cannot be read.",
    )


def _work(ctx: UserProject, workflow: str, slot: int) -> Work:
    packet = ctx.engine.work_packet(workflow, slot, ctx.actor)
    return Work(slot=ctx.engine.slot(workflow, slot), packet=packet, shown=packet.shown())


@router.post(
    "/next",
    response_model=Work,
    summary="Ask for work",
    responses={204: {"description": "There is no work for you in this workflow right now."}},
)
def next_work(workflow: str, ctx: UserProject) -> Work | Response:
    """Get the slot you are working on, or, if you hold none, claim the next one you are
    allowed to take (slots of later stages first, then the oldest item). Answers 204 when
    there is nothing for you."""
    slot = ctx.engine.request_work(workflow, ctx.actor)
    if slot is None:
        return Response(status_code=204)
    return _work(ctx, workflow, slot.slot)


@router.post(
    "/slots/{slot}/claim",
    response_model=Work,
    summary="Claim a particular slot",
    responses=errors(403, 409),
)
def claim_slot(workflow: str, slot: int, ctx: UserProject) -> Work:
    """Claim one particular open slot, for an interface that lets people choose. It is
    refused (403) if you may not take it: you do not hold a role for the stage, or you have
    already labeled or released this item."""
    claimed = ctx.engine.claim(workflow, slot, ctx.actor)
    return _work(ctx, workflow, claimed.slot)


@router.get(
    "/slots/{slot}",
    response_model=Work,
    summary="Show the work of a slot you hold",
    responses=errors(403),
)
def get_work(workflow: str, slot: int, ctx: UserProject) -> Work:
    """Show what to display for a slot you have claimed. Slots held by someone else, or no
    longer held, are refused (403)."""
    return _work(ctx, workflow, slot)


@router.post(
    "/slots/{slot}/submit",
    status_code=201,
    response_model=SubmitOut,
    summary="Submit a label",
    responses=errors(403),
)
def submit_label(workflow: str, slot: int, body: SubmitIn, ctx: UserProject) -> SubmitOut:
    """Submit the label for a slot you hold, with the references to what you showed. A label
    that does not fit the label task is rejected with every problem; nothing is recorded and
    you still hold the slot."""
    label_id = ctx.engine.submit(
        workflow, slot, ctx.actor, body.label, body.shown, body.duration_ms
    )
    return SubmitOut(label_id=label_id)


@router.post(
    "/slots/{slot}/release",
    response_model=Slot,
    summary="Give a slot back",
    responses=errors(403),
)
def release_slot(workflow: str, slot: int, body: ReleaseIn, ctx: UserProject) -> Slot:
    """Give a slot you hold back to the pool, with a reason. You will not be offered this
    item in this stage again."""
    ctx.engine.release(workflow, slot, ctx.actor, body.reason)
    return ctx.engine.slot(workflow, slot)
