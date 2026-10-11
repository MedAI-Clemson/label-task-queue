"""Roles: who holds which role in a project. Every change is logged with a reason."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from labelflow.api.deps import AdminProject, RuntimeDep
from labelflow.api.errors import errors
from labelflow.engine import NotAllowed, Role

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


class RoleChangeIn(BaseModel):
    user: str = Field(min_length=1, max_length=64, description="The person's username.")
    reason: str = Field(
        min_length=1, max_length=1000, description="Why. It is kept in the record of the change."
    )


class RoleChangeOut(Role):
    """The role after the change, and the staffing warnings the change caused."""

    warnings: list[str] = Field(
        description="Stages of running workflows whose roles now have too few people to fill "
        "them. Empty when every stage is staffed."
    )


@router.get("", response_model=list[Role], summary="List roles and who holds them")
def list_roles(ctx: AdminProject) -> list[Role]:
    """List the project's roles, each with the people who hold it now."""
    return ctx.engine.roles()


@router.post(
    "", status_code=201, response_model=Role, summary="Define a role", responses=errors(409)
)
def define_role(body: RoleIn, ctx: AdminProject) -> Role:
    """Define a role that workflow stages can be assigned to. Roles are shared by every
    workflow of the project; nobody holds the role until you grant it."""
    ctx.engine.define_role(body.role, ctx.actor)
    return ctx.engine.role(body.role)


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
    return RoleChangeOut(**ctx.engine.role(role).model_dump(), warnings=warnings)


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
    return RoleChangeOut(**ctx.engine.role(role).model_dump(), warnings=warnings)
