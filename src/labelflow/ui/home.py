"""The home page: the projects a person can see, and the workflows with work waiting.

A labeler picks the workflow to work on (decided), so each workflow shows how many open
slots this person could claim now. An administrator sees every project and workflow, with
links to the admin pages.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from labelflow.api.deps import RuntimeDep, User
from labelflow.engine import Engine, Workflow
from labelflow.engine.eligibility import load_person, why_not
from labelflow.engine.queries import active_config, active_pause
from labelflow.ui.app import render

router = APIRouter()


def waiting_for(engine: Engine, workflow: str, user: str) -> tuple[int, int | None]:
    """How many open slots this person may claim in the workflow now, and the slot they
    already hold there, if any. Paused stages and workflows count for nothing."""
    store = engine.store
    with store.reading():
        _, config = active_config(store, workflow)
        person = load_person(store, workflow, user)
        held = next((row["slot"] for row in person.claims.values()), None)
        if not person.roles or active_pause(store, workflow) is not None:
            return 0, held
        count = 0
        for slot in store.select("slots", workflow=workflow, status="open"):
            stage = slot["stage"]
            if stage not in config.stage_names or active_pause(store, workflow, stage) is not None:
                continue
            if why_not(person, slot, config.stage(stage).roles) is None:
                count += 1
        return count, held


def workflows_for(engine: Engine, user: User) -> list[dict[str, Any]]:
    """The workflows of a project as one person sees them: all of them for an admin, and
    for anyone else the ones with a stage assigned to a role they hold."""
    mine = engine.user_roles(user.user_id)
    rows = []
    for workflow in engine.workflows():
        roles = {role for stage in workflow.stages for role in stage.roles}
        if not user.is_admin and not roles & mine:
            continue
        waiting, held = waiting_for(engine, workflow.workflow, user.user_id)
        rows.append(_row(workflow, roles & mine, waiting, held))
    return rows


def _row(workflow: Workflow, my_roles: set[str], waiting: int, held: int | None) -> dict[str, Any]:
    return {
        "workflow": workflow,
        "name": workflow.workflow,
        "my_roles": sorted(my_roles),
        "waiting": waiting,
        "held": held,
        "paused": workflow.paused is not None,
        "paused_stages": [stage.name for stage in workflow.stages if stage.paused is not None],
    }


@router.get("/")
def home(request: Request, user: User, runtime: RuntimeDep) -> Response:
    held = runtime.roles_of(user.user_id)
    projects = []
    for project in runtime.projects.list_all():
        if not user.is_admin and project.name not in held:
            continue
        engine = runtime.engine_for(project.name)
        projects.append(
            {
                "name": project.name,
                "my_roles": held.get(project.name, []),
                "workflows": workflows_for(engine, user),
            }
        )
    return render(request, "home.html", user=user, projects=projects)
