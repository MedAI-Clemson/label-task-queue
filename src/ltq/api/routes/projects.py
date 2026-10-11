"""Projects: the list, and creating one."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ltq.api.deps import AdminUser, RuntimeDep, User
from ltq.api.errors import errors

router = APIRouter(prefix="/projects")


class ProjectIn(BaseModel):
    name: str = Field(
        max_length=64,
        description="The project name: letters, digits and underscores, starting with a letter.",
    )


class ProjectOut(BaseModel):
    name: str = Field(description="The project name.")
    created_by: str | None = Field(description="Who created the project.")
    created_at: datetime = Field(description="When the project was created (UTC).")
    your_roles: list[str] = Field(description="The roles you hold in this project now.")


@router.post(
    "",
    status_code=201,
    response_model=ProjectOut,
    tags=["admin: projects"],
    summary="Create a project",
    responses=errors(401, 403, 409, 422),
)
def create_project(body: ProjectIn, admin: AdminUser, runtime: RuntimeDep) -> ProjectOut:
    """Create an empty project. Its roles, datasets and workflows are added afterwards."""
    project = runtime.projects.create(body.name, admin.user_id)
    return ProjectOut(
        name=project.name,
        created_by=project.created_by,
        created_at=project.created_at,
        your_roles=runtime.projects.roles_of(admin.user_id).get(project.name, []),
    )


@router.get(
    "",
    response_model=list[ProjectOut],
    tags=["projects"],
    summary="List projects",
    responses=errors(401),
)
def list_projects(user: User, runtime: RuntimeDep) -> list[ProjectOut]:
    """List the projects you can see: every project if you are an administrator, otherwise the
    projects where you hold a role."""
    held = runtime.projects.roles_of(user.user_id)
    return [
        ProjectOut(
            name=project.name,
            created_by=project.created_by,
            created_at=project.created_at,
            your_roles=held.get(project.name, []),
        )
        for project in runtime.projects.list_all()
        if user.is_admin or project.name in held
    ]
