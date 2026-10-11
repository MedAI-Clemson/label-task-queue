"""Logging in and out."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from ltq.api.deps import COOKIE, RuntimeDep, User, session_token
from ltq.api.errors import errors
from ltq.api.identity import Principal

router = APIRouter(prefix="/auth", tags=["login"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, description="Your username.")
    password: str = Field(min_length=1, max_length=256, description="Your password.")


class UserOut(BaseModel):
    user_id: str = Field(description="The username. It is the id that labels and events record.")
    display_name: str = Field(description="The name shown to people.")
    is_admin: bool = Field(description="Whether this account administers every project.")

    @classmethod
    def of(cls, principal: Principal) -> UserOut:
        return cls(
            user_id=principal.user_id,
            display_name=principal.display_name,
            is_admin=principal.is_admin,
        )


class LoginOut(BaseModel):
    token: str = Field(
        description="Your login token. Send it as `Authorization: Bearer <token>` on every "
        "request. It is also set as a login cookie for the web pages."
    )
    token_type: Literal["bearer"] = Field(description="Always `bearer`.")
    expires_at: datetime = Field(description="When this login stops working (UTC).")
    user: UserOut = Field(description="The account that is now logged in.")


@router.post(
    "/login",
    response_model=LoginOut,
    summary="Log in",
    responses=errors(401, 422),
)
def login(body: LoginIn, response: Response, runtime: RuntimeDep) -> LoginOut:
    """Check a username and password. On success you get a token to send with every later
    request, and the same token is set as a login cookie."""
    result = runtime.identity.login(body.username, body.password)
    settings = runtime.settings
    response.set_cookie(
        COOKIE,
        result.token,
        max_age=int(settings.session_lifetime.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return LoginOut(
        token=result.token,
        token_type="bearer",
        expires_at=result.expires_at,
        user=UserOut.of(result.principal),
    )


@router.post("/logout", status_code=204, summary="Log out")
def logout(
    response: Response,
    token: Annotated[str | None, Depends(session_token)],
    runtime: RuntimeDep,
) -> None:
    """End your login and remove the login cookie. It is safe to call when you are already
    logged out."""
    runtime.identity.logout(token)
    response.delete_cookie(COOKIE, path="/")


@router.get("/me", response_model=UserOut, summary="Who am I?", responses=errors(401))
def me(user: User) -> UserOut:
    """Show the account you are logged in as."""
    return UserOut.of(user)
