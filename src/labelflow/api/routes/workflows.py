"""Workflows: check a config, start it, look at it, and edit it while it runs.

The API works with whole config documents only: there are no endpoints that build a workflow
piece by piece. A config is sent as YAML text or as the same document written as JSON.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from labelflow.api.deps import AdminProject, UserProject
from labelflow.api.errors import Forbidden, Unprocessable, errors
from labelflow.engine import ChangePreview, Progress, Workflow, WorkflowDetail
from labelflow.engine.config import ConfigReport, resolved_yaml, to_mermaid, validate_config

router = APIRouter(prefix="/projects/{project}/workflows", responses=errors(401, 403, 404, 422))

ConfigDocument = Annotated[
    str | dict[str, Any],
    Field(
        description="The workflow config: YAML text, or the same document as a JSON object "
        "(`workflow`, `dataset`, `label_task`, `stages`, `final`).",
        examples=["workflow: xray_main\ndataset: xray_batch_1\nlabel_task: image_ontology@1\n"],
    ),
]


class ConfigIn(BaseModel):
    config: ConfigDocument


class ProblemOut(BaseModel):
    where: str = Field(description="Where to look, such as `stage 'annotate', key 'sample'`.")
    message: str = Field(description="What is wrong, and what to do about it.")


class ValidationOut(BaseModel):
    ok: bool = Field(description="True when the config has no errors (warnings are allowed).")
    errors: list[ProblemOut] = Field(description="Every mistake found. Empty when `ok`.")
    warnings: list[ProblemOut] = Field(
        description="Things worth knowing, such as a stage whose roles have too few people."
    )
    resolved_yaml: str | None = Field(
        description="The config with every default written out. Empty when there are errors."
    )
    diagram: str | None = Field(
        description="A Mermaid flowchart of the workflow. Empty when there are errors."
    )
    report: str = Field(description="The errors and warnings as one readable text.")


class StartIn(BaseModel):
    config: ConfigDocument
    seed: int | None = Field(
        default=None,
        description="A whole number from 0 to 2**63 - 1 that fixes every random draw, so the "
        "sampling can be checked later. Leave it out to pick one at random; it is returned.",
    )


class StartOut(BaseModel):
    workflow: str = Field(description="The workflow's name.")
    seed: int = Field(description="The seed in use. Keep it: it makes the draws recomputable.")
    config_version: int = Field(description="The config version now in force (always 1).")
    warnings: list[str] = Field(
        description="Stages whose roles have too few people to fill them. Empty when all are "
        "staffed."
    )


class WorkflowOut(Workflow):
    """A workflow as it runs now, with its seed shown to administrators only."""

    seed: int | None = Field(
        description="The seed that fixes the workflow's random draws. Only administrators see "
        "it: knowing it would tell labelers which of their items will be reviewed."
    )


class WorkflowDetailOut(WorkflowOut):
    """A workflow with its config in force and how far it has got."""

    resolved_yaml: str = Field(description="The active config with every default written out.")
    diagram: str = Field(description="A Mermaid flowchart of the active config.")
    progress: Progress = Field(description="Items by status and slots by stage.")


class VersionOut(BaseModel):
    version: int = Field(description="The config version number (1 is the first).")
    active: bool = Field(description="Whether this version is in force now.")
    activated_by: str | None = Field(description="Who started the workflow or made the edit.")
    activated_at: datetime = Field(description="When it was activated (UTC).")
    config_hash: str = Field(description="A fingerprint of the config, as recorded in the log.")
    reason: str | None = Field(description="Why the edit was made (empty for version 1).")
    diff: str | None = Field(description="What the edit changed, as a diff (empty for version 1).")
    resolved_yaml: str = Field(description="This version's config with every default written out.")


class PreviewOut(BaseModel):
    can_activate: bool = Field(description="True when this edit could be activated as it is.")
    preview: ChangePreview = Field(
        description="What the edit changes and touches: each change, why it would be refused, "
        "warnings, a diff, and counts of the items and slots affected."
    )
    text: str = Field(description="The same preview as readable text.")


class EditIn(BaseModel):
    config: ConfigDocument
    base_version: int = Field(
        ge=1,
        description="The config version you previewed the edit against. If someone has edited "
        "the workflow since, the edit is refused and you should preview it again.",
    )
    reason: str = Field(
        default="", max_length=1000, description="Why you are making the edit (kept in the record)."
    )


class EditOut(BaseModel):
    workflow: str = Field(description="The workflow's name.")
    config_version: int = Field(description="The new config version, now in force.")
    warnings: list[str] = Field(description="Stages whose roles have too few people to fill them.")


def check_config(ctx: AdminProject, source: str | dict[str, Any]) -> ConfigReport:
    """Check a config against the project's roles, datasets and label tasks."""
    return validate_config(source, ctx.engine.validation_context())


def _problems(problems: Any) -> list[ProblemOut]:
    return [ProblemOut(where=p.where, message=p.message) for p in problems]


def _as_seen_by(ctx: UserProject, workflow: Workflow) -> dict[str, Any]:
    """The workflow's fields, with the seed left out for anyone but an administrator."""
    fields = workflow.model_dump(exclude={"config"})
    return fields if ctx.user.is_admin else fields | {"seed": None}


def _holds_a_role_in(ctx: UserProject, workflow: Workflow) -> bool:
    used = {role for stage in workflow.stages for role in stage.roles}
    return ctx.user.is_admin or bool(ctx.engine.user_roles(ctx.actor) & used)


@router.post(
    "/validate",
    response_model=ValidationOut,
    tags=["admin: workflows"],
    summary="Check a workflow config",
)
def validate_workflow(body: ConfigIn, ctx: AdminProject) -> ValidationOut:
    """Check a config without starting anything. It reports every mistake and warning in
    plain language, using this project's roles, datasets and label tasks, and gives back the
    config with defaults written out and a diagram. A config with mistakes is answered with
    `ok: false`, not an error."""
    report = check_config(ctx, body.config)
    config = report.config
    return ValidationOut(
        ok=report.ok,
        errors=_problems(report.errors),
        warnings=_problems(report.warnings),
        resolved_yaml=resolved_yaml(config) if config else None,
        diagram=to_mermaid(config) if config else None,
        report=report.render(),
    )


@router.post(
    "",
    status_code=201,
    response_model=StartOut,
    tags=["admin: workflows"],
    summary="Start a workflow",
    responses=errors(409),
)
def start_workflow(body: StartIn, ctx: AdminProject) -> StartOut:
    """Start a workflow from a config. Every item of its dataset is admitted at once, and
    slots open for the first stage. The config must be valid (see the check endpoint)."""
    report = check_config(ctx, body.config)
    if report.config is None:
        count = len(report.errors)
        listed = " ".join(f"{i}. {problem}" for i, problem in enumerate(report.errors, start=1))
        raise Unprocessable(
            f"The config has {count} error{'' if count == 1 else 's'}. {listed}",
            [str(problem) for problem in report.errors],
        )
    seed = body.seed if body.seed is not None else secrets.randbits(63)
    warnings = ctx.engine.start_workflow(report.config, seed, ctx.actor)
    return StartOut(workflow=report.config.workflow, seed=seed, config_version=1, warnings=warnings)


@router.get(
    "",
    response_model=list[WorkflowOut],
    tags=["admin: workflows", "labeler"],
    summary="List workflows",
)
def list_workflows(ctx: UserProject) -> list[WorkflowOut]:
    """List the project's workflows with their stages and what is paused. Administrators see
    all of them; anyone else sees the workflows with a stage that one of their roles is
    assigned to."""
    return [
        WorkflowOut(**_as_seen_by(ctx, w))
        for w in ctx.engine.workflows()
        if _holds_a_role_in(ctx, w)
    ]


@router.get(
    "/{workflow}",
    response_model=WorkflowDetailOut,
    tags=["admin: workflows", "labeler"],
    summary="Show a workflow",
)
def get_workflow(workflow: str, ctx: UserProject) -> WorkflowDetailOut:
    """Show a workflow: the config in force with every default written out, its diagram, its
    progress (items by status, slots by stage) and what is paused. Administrators can see
    every workflow; anyone else only those they hold a role in."""
    detail: WorkflowDetail = ctx.engine.workflow(workflow)
    if not _holds_a_role_in(ctx, detail):
        raise Forbidden("You do not hold a role in this workflow.")
    return WorkflowDetailOut(
        **_as_seen_by(ctx, detail),
        resolved_yaml=resolved_yaml(detail.config),
        diagram=to_mermaid(detail.config),
    )


@router.get(
    "/{workflow}/versions",
    response_model=list[VersionOut],
    tags=["admin: workflows"],
    summary="List a workflow's config versions",
)
def list_versions(workflow: str, ctx: AdminProject) -> list[VersionOut]:
    """List every config version of a workflow, oldest first, with who activated it, why,
    and what it changed."""
    return [
        VersionOut(**v.model_dump(exclude={"config"}), resolved_yaml=resolved_yaml(v.config))
        for v in ctx.engine.config_versions(workflow)
    ]


@router.post(
    "/{workflow}/preview-edit",
    response_model=PreviewOut,
    tags=["admin: workflows"],
    summary="Preview an edit to a running workflow",
)
def preview_edit(workflow: str, body: ConfigIn, ctx: AdminProject) -> PreviewOut:
    """Show what activating a new config would change, without changing anything: each
    change, whether it can be applied to a running workflow, a diff, and how many items and
    slots it touches. Send the whole new config."""
    preview = ctx.engine.preview_edit(workflow, body.config)
    return PreviewOut(can_activate=preview.ok, preview=preview, text=preview.render())


@router.post(
    "/{workflow}/edits",
    status_code=201,
    response_model=EditOut,
    tags=["admin: workflows"],
    summary="Edit a running workflow",
    responses=errors(409),
)
def activate_edit(workflow: str, body: EditIn, ctx: AdminProject) -> EditOut:
    """Make a new config the workflow's next version. It governs only decisions made from
    now on; existing slots, labels and final labels stay as they are. Preview the edit
    first and send the version you previewed it against: if the workflow has been edited
    since, the edit is refused (409). An edit that cannot be applied to a running workflow
    is refused with every reason."""
    warnings = ctx.engine.activate_edit(
        workflow, body.config, ctx.actor, body.base_version, body.reason
    )
    return EditOut(workflow=workflow, config_version=body.base_version + 1, warnings=warnings)
