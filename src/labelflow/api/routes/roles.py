"""Roles: who holds which role in a project. Every change is logged with a reason."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from labelflow.api import reads
from labelflow.api.deps import AdminProject, RuntimeDep
from labelflow.api.errors import errors
from labelflow.engine import NotAllowed

router = APIRouter(
    prefix="/projects/{project}/roles",
    tags=["admin: roles"],
    responses=errors(401, 403, 404, 422),
)


class RoleIn(BaseModel):
    role: str = Field(
        max_length=64,
        description="The new role's name: letters, digits and underscores, starting with a letter.",
    )


class RoleOut(BaseModel):
    role: str = Field(description="The role's name.")
    defined_by: str | None = Field(description="Who defined the role.")
    defined_at: datetime = Field(description="When the role was defined (UTC).")
    holders: list[str] = Field(description="The people who hold the role now.")


class RoleChangeIn(BaseModel):
    user: str = Field(min_length=1, max_length=64, description="The person's username.")
    reason: str = Field(
        min_length=1, max_length=1000, description="Why. It is kept in the record of the change."
    )


class RoleChangeOut(BaseModel):
    role: str = Field(description="The role that changed.")
    holders: list[str] = Field(description="The people who hold the role now.")
    warnings: list[str] = Field(
        description="Stages of running workflows whose roles now have too few people to fill "
        "them. Empty when every stage is staffed."
    )


@router.get("", response_model=list[RoleOut], summary="List roles and who holds them")
def list_roles(ctx: AdminProject) -> list[RoleOut]:
    """List the project's roles, each with the people who hold it now."""
    return [RoleOut(**row) for row in reads.roles(ctx.engine.store)]


@router.post(
    "", status_code=201, response_model=RoleOut, summary="Define a role", responses=errors(409)
)
def define_role(body: RoleIn, ctx: AdminProject) -> RoleOut:
    """Define a role that workflow stages can be assigned to. Roles are shared by every
    workflow of the project; nobody holds the role until you grant it."""
    ctx.engine.define_role(body.role, ctx.actor)
    return RoleOut(**reads.role(ctx.engine.store, body.role))


@router.post(
    "/{role}/grant",
    response_model=RoleChangeOut,
    summary="Give someone a role",
    responses=errors(409),
)
def grant_role(
    role: str, body: RoleChangeIn, ctx: AdminProject, runtime: RuntimeDep
) -> RoleChangeOut:
    """Give a person a role, with a reason. It applies from their next request for work. The
    reply warns when a running workflow still has too few people for a stage."""
    account = runtime.accounts.require(body.user)
    if not account.active:
        raise NotAllowed(f"The account '{body.user}' is deactivated, so it cannot be given a role.")
    warnings = ctx.engine.grant_role(body.user, role, ctx.actor, body.reason)
    return RoleChangeOut(
        role=role, holders=reads.role(ctx.engine.store, role)["holders"], warnings=warnings
    )


@router.post(
    "/{role}/revoke",
    response_model=RoleChangeOut,
    summary="Take a role away",
    responses=errors(409),
)
def revoke_role(role: str, body: RoleChangeIn, ctx: AdminProject) -> RoleChangeOut:
    """Take a role away from a person, with a reason. A slot they already claimed stays
    theirs; the change applies from their next request for work. The reply warns when a
    running workflow now has too few people for a stage."""
    warnings = ctx.engine.revoke_role(body.user, role, ctx.actor, body.reason)
    return RoleChangeOut(
        role=role, holders=reads.role(ctx.engine.store, role)["holders"], warnings=warnings
    )
