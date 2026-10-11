"""What every endpoint needs: the runtime, who is asking, and which project they mean.

``current_user`` accepts the login token as an ``Authorization: Bearer`` header or as the
login cookie (the header wins when both are sent). Admins administer every project. Anyone
else reaches a project only while they hold a role in it; a project they hold none in is
"not found" to them, exactly like a project that does not exist, so the answer never says
which projects there are. Inside a project, the engine decides what work they may claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from labelflow.api.errors import Forbidden, NotLoggedIn
from labelflow.api.identity import Principal
from labelflow.api.projects import unknown_project
from labelflow.api.runtime import Runtime
from labelflow.engine import Engine

COOKIE = "labelflow_session"

_bearer = HTTPBearer(
    auto_error=False,
    description="The token from `POST /auth/login`, sent as `Authorization: Bearer <token>`.",
)
_cookie = APIKeyCookie(
    name=COOKIE,
    auto_error=False,
    description="The login cookie set by `POST /auth/login` (used by the web pages).",
)


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    runtime.ready()
    return runtime


def session_token(
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    cookie: Annotated[str | None, Depends(_cookie)],
) -> str | None:
    """The login token the request carries, if any."""
    return bearer.credentials if bearer is not None else cookie


def current_user(
    request: Request,
    token: Annotated[str | None, Depends(session_token)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> Principal:
    principal = runtime.identity.authenticate(token)
    if principal is None:
        if token is None:
            raise NotLoggedIn("You are not logged in. Log in with POST /auth/login.")
        raise NotLoggedIn(
            "This login is not valid any more: it has expired, or the account was deactivated. "
            "Log in again."
        )
    request.state.user = principal  # the error handlers word some answers by who asks
    return principal


def admin_user(user: Annotated[Principal, Depends(current_user)]) -> Principal:
    if not user.is_admin:
        raise Forbidden("This needs an administrator account.")
    return user


@dataclass(frozen=True)
class ProjectContext:
    """One project, as one logged-in person sees it."""

    name: str
    engine: Engine
    user: Principal

    @property
    def actor(self) -> str:
        """The id the engine records as the person acting."""
        return self.user.user_id


def project_for_user(
    project: str,
    user: Annotated[Principal, Depends(current_user)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> ProjectContext:
    """The project, for someone who administers it or holds a role in it."""
    runtime.projects.require(project)
    engine = runtime.engine_for(project)
    if not user.is_admin and not engine.user_roles(user.user_id):
        raise unknown_project(project)
    return ProjectContext(project, engine, user)


def project_for_admin(
    project: str,
    user: Annotated[Principal, Depends(admin_user)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> ProjectContext:
    runtime.projects.require(project)
    return ProjectContext(project, runtime.engine_for(project), user)


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
User = Annotated[Principal, Depends(current_user)]
AdminUser = Annotated[Principal, Depends(admin_user)]
UserProject = Annotated[ProjectContext, Depends(project_for_user)]
AdminProject = Annotated[ProjectContext, Depends(project_for_admin)]
