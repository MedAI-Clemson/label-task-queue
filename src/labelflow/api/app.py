"""The application factory.

``create_app(settings)`` builds the web app; tests give it a temporary database, and
``labelflow.api:app`` is the one uvicorn serves, built from the environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.routing import APIRoute

from labelflow.api.errors import add_error_handlers
from labelflow.api.limits import BodyLimit
from labelflow.api.routes import (
    accounts,
    actions,
    auth,
    datasets,
    feedback,
    labeling,
    projects,
    roles,
    workflows,
)
from labelflow.api.runtime import Runtime
from labelflow.api.settings import Settings
from labelflow.engine import utc_now
from labelflow.labeltask import LabelTaskRegistry

DESCRIPTION = """\
Run labeling workflows over images, with a record of every decision.

**How to use it.** Log in with `POST /auth/login` and send the token you get as
`Authorization: Bearer <token>`. Administrators create accounts, projects, roles, datasets and
workflows; labelers ask a workflow for work, label it, and submit.

**Words used here.** A *project* holds roles, datasets and workflows. A *workflow* is a config
that says which *stages* an item goes through. A *slot* is one label an item needs in one
stage; a person *claims* a slot, then *submits* a label or *releases* it. Every change is
written to the project's event log with the rule that caused it.
"""

TAGS = [
    {"name": "login", "description": "Log in, log out, and see who you are."},
    {"name": "projects", "description": "The projects you can see."},
    {"name": "labeler", "description": "Ask for work, label it, and submit or give it back."},
    {
        "name": "admin: accounts",
        "description": "Create accounts, deactivate them, reset passwords.",
    },
    {"name": "admin: projects", "description": "Create projects."},
    {"name": "admin: roles", "description": "Define roles and say who holds them."},
    {"name": "admin: datasets", "description": "Create datasets and import their items."},
    {
        "name": "admin: workflows",
        "description": "Check, start, look at and edit workflows. Whole config documents only.",
    },
    {
        "name": "admin: actions",
        "description": "Exclude an item from a workflow, cancel a stage's open slots, or "
        "pause and resume claims in a stage or a whole workflow.",
    },
    {
        "name": "admin: feedback",
        "description": "Explain an item, reports, exports (CSV or Parquet), and the audit view.",
    },
    {"name": "health", "description": "Is the service running?"},
]

ROUTERS = (auth, accounts, projects, roles, datasets, workflows, labeling, actions, feedback)


def _operation_id(route: APIRoute) -> str:
    return route.name


def create_app(
    settings: Settings | None = None,
    *,
    label_tasks: LabelTaskRegistry | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    """Build the app. ``label_tasks`` replaces the ontology folder (for tests and for
    deployments that register their own label tasks); ``clock`` is for tests."""
    runtime = Runtime(settings or Settings.from_env(), label_tasks, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime.ready()  # stop at startup, with a clear message, if the setup is wrong
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(
        title="label-task-queue",
        summary="Auditable labeling workflows for research data",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    app.state.runtime = runtime
    app.add_middleware(BodyLimit, limit=runtime.settings.max_request_bytes)
    add_error_handlers(app)

    @app.get("/health", tags=["health"], summary="Is the service running?")
    def health() -> dict[str, str]:
        """Report that the service is running. It needs no login."""
        return {"status": "ok"}

    for module in ROUTERS:
        app.include_router(module.router)
    return app
