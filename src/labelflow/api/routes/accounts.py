"""Accounts, for administrators. An admin creates every account; nobody signs themselves up."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from labelflow.api.accounts import MAX_PASSWORD_LENGTH
from labelflow.api.deps import AdminUser, RuntimeDep
from labelflow.api.errors import errors
from labelflow.engine import NotAllowed

router = APIRouter(
    prefix="/accounts",
    tags=["admin: accounts"],
    responses=errors(401, 403, 422),
)

DEACTIVATED = "account deactivated"


class AccountIn(BaseModel):
    username: str = Field(
        max_length=64,
        description="The username: lowercase letters, digits, '_', '.' or '-'. It cannot be "
        "changed later, and it is the id that labels and events record.",
    )
    password: str = Field(
        max_length=MAX_PASSWORD_LENGTH,
        description="The first password, at least 8 characters. Tell the person to change it "
        "or ask for a reset.",
    )
    display_name: str = Field(
        default="", max_length=100, description="The name shown to people (default: the username)."
    )
    is_admin: bool = Field(
        default=False, description="Whether the account administers every project."
    )


class AccountOut(BaseModel):
    user_id: str = Field(description="The username.")
    display_name: str = Field(description="The name shown to people.")
    is_admin: bool = Field(description="Whether the account administers every project.")
    active: bool = Field(description="False once the account is deactivated; it cannot log in.")
    created_by: str | None = Field(
        description="Who created the account (empty for the first admin)."
    )
    created_at: datetime = Field(description="When the account was created (UTC).")


class DeactivatedOut(AccountOut):
    revoked_roles: dict[str, list[str]] = Field(
        description="The roles taken away from the account, by project."
    )
    warnings: list[str] = Field(
        description="Stages of running workflows whose roles now have too few people to fill "
        "them. Empty when every stage is still staffed."
    )


class PasswordIn(BaseModel):
    password: str = Field(
        max_length=MAX_PASSWORD_LENGTH, description="The new password, at least 8 characters."
    )


@router.post("", status_code=201, response_model=AccountOut, summary="Create an account")
def create_account(body: AccountIn, admin: AdminUser, runtime: RuntimeDep) -> AccountOut:
    """Create an account. The person can log in with this username and password straight away."""
    account = runtime.accounts.create(
        body.username, body.password, body.display_name, body.is_admin, created_by=admin.user_id
    )
    return AccountOut(**vars(account))


@router.get("", response_model=list[AccountOut], summary="List accounts")
def list_accounts(admin: AdminUser, runtime: RuntimeDep) -> list[AccountOut]:
    """List every account, including deactivated ones."""
    return [AccountOut(**vars(account)) for account in runtime.accounts.list_all()]


@router.post(
    "/{username}/deactivate",
    response_model=DeactivatedOut,
    summary="Deactivate an account",
    responses=errors(404, 409),
)
def deactivate_account(username: str, admin: AdminUser, runtime: RuntimeDep) -> DeactivatedOut:
    """Stop an account from logging in, end its current logins, and take away every role it
    holds, in every project, so it no longer counts as someone who can label. What the
    person did stays in the record. You cannot deactivate your own account."""
    if username == admin.user_id:
        raise NotAllowed("You cannot deactivate your own account. Ask another administrator.")
    account = runtime.accounts.deactivate(username)
    revoked = runtime.roles_of(username)
    warnings: list[str] = []
    for project, roles in revoked.items():
        engine = runtime.engine_for(project)
        for role in roles:  # each call reports the project's staffing after that revocation
            latest = engine.revoke_role(username, role, admin.user_id, DEACTIVATED)
        warnings += [f"project {project}, {warning}" for warning in latest]
    return DeactivatedOut(**vars(account), revoked_roles=revoked, warnings=warnings)


@router.post(
    "/{username}/password",
    status_code=204,
    summary="Reset a password",
    responses=errors(404),
)
def reset_password(username: str, body: PasswordIn, admin: AdminUser, runtime: RuntimeDep) -> None:
    """Set a new password for an account and end its current logins."""
    runtime.accounts.set_password(username, body.password)
