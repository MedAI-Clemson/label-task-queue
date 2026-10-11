"""The login and logout pages. They set and clear the same cookie the API's login does."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from labelflow.api.deps import COOKIE, RuntimeDep, session_token
from labelflow.api.errors import NotLoggedIn
from labelflow.ui.app import redirect, render, safe_next

router = APIRouter()


@router.get("/login")
def login_page(request: Request, runtime: RuntimeDep, next: str | None = None) -> Response:
    if runtime.identity.authenticate(request.cookies.get(COOKIE)) is not None:
        return RedirectResponse(safe_next(request, next), status_code=303)
    return render(request, "login.html", next=next or "")


@router.post("/login")
def login(
    request: Request,
    runtime: RuntimeDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    try:
        if not username or not password:
            raise NotLoggedIn("Enter your username and password.")
        result = runtime.identity.login(username, password)
    except NotLoggedIn as error:
        return render(request, "login.html", status_code=401, error=error.detail, next=next)
    response = RedirectResponse(safe_next(request, next), status_code=303)
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
    return response


@router.post("/logout")
def logout(
    request: Request,
    runtime: RuntimeDep,
    token: Annotated[str | None, Depends(session_token)],
) -> Response:
    runtime.identity.logout(token)
    response = redirect(request, "/login", "You are logged out.")
    response.delete_cookie(COOKIE, path="/")
    return response
