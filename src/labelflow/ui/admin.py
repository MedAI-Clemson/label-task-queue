"""The admin pages: accounts, projects (roles, datasets, workflows), and one workflow.

Every page is a form or two over the same operations the API offers; the engine's errors
are shown on the page the form came from. The workflow pages check a config as it is typed
(an HTMX post that returns a fragment with the errors, warnings, resolved YAML and diagram),
start it, show its progress, pause and resume it, preview and activate edits, explain an
item, and link to the exports.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from pydantic import ValidationError

from labelflow.api.deps import AdminProject, AdminUser, RuntimeDep
from labelflow.api.errors import ApiError, Unprocessable, status_for
from labelflow.api.media import CannotHash, content_hash
from labelflow.api.routes.datasets import ItemIn
from labelflow.engine import EngineError, NewItem, NotAllowed
from labelflow.engine.config import ConfigReport, resolved_yaml, to_mermaid, validate_config
from labelflow.ui.app import CsrfChecked, redirect, render

router = APIRouter(prefix="/admin")

Reason = Annotated[str, Form()]


def failure(error: Exception) -> tuple[int, str, list[str]]:
    """An engine or API error as (status, message, problems), for showing on a page."""
    if isinstance(error, ApiError):
        return error.status, error.detail, error.problems
    assert isinstance(error, EngineError)
    return status_for(error), str(error), list(getattr(error, "problems", ()))


# --- accounts -----------------------------------------------------------------------------------


def _accounts_page(
    request: Request, admin: AdminUser, runtime: RuntimeDep, **extra: Any
) -> Response:
    return render(
        request,
        "admin/accounts.html",
        user=admin,
        accounts=runtime.accounts.list_all(),
        **extra,
    )


@router.get("/accounts")
def accounts(request: Request, admin: AdminUser, runtime: RuntimeDep) -> Response:
    return _accounts_page(request, admin, runtime)


@router.post("/accounts")
def create_account(
    request: Request,
    admin: AdminUser,
    runtime: RuntimeDep,
    _: CsrfChecked,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    is_admin: Annotated[str, Form()] = "",
) -> Response:
    try:
        account = runtime.accounts.create(
            username.strip(),
            password,
            display_name,
            is_admin == "on",
            created_by=admin.user_id,
        )
    except EngineError as error:
        status, detail, problems = failure(error)
        return _accounts_page(
            request, admin, runtime, status_code=status, error=detail, problems=problems
        )
    return redirect(request, "/admin/accounts", f"Created the account '{account.user_id}'.")


@router.post("/accounts/{username}/deactivate")
def deactivate_account(
    username: str, request: Request, admin: AdminUser, runtime: RuntimeDep, _: CsrfChecked
) -> Response:
    try:
        if username == admin.user_id:
            raise NotAllowed("You cannot deactivate your own account. Ask another administrator.")
        runtime.accounts.deactivate(username)
    except EngineError as error:
        status, detail, problems = failure(error)
        return _accounts_page(
            request, admin, runtime, status_code=status, error=detail, problems=problems
        )
    return redirect(request, "/admin/accounts", f"Deactivated the account '{username}'.")


@router.post("/accounts/{username}/password")
def reset_password(
    username: str,
    request: Request,
    admin: AdminUser,
    runtime: RuntimeDep,
    _: CsrfChecked,
    password: Annotated[str, Form()] = "",
) -> Response:
    try:
        runtime.accounts.set_password(username, password)
    except EngineError as error:
        status, detail, problems = failure(error)
        return _accounts_page(
            request, admin, runtime, status_code=status, error=detail, problems=problems
        )
    return redirect(
        request, "/admin/accounts", f"Set a new password for '{username}'; their logins ended."
    )


# --- projects -----------------------------------------------------------------------------------


@router.get("/projects")
def projects(request: Request, admin: AdminUser, runtime: RuntimeDep) -> Response:
    return render(request, "admin/projects.html", user=admin, projects=runtime.projects.list_all())


@router.post("/projects")
def create_project(
    request: Request,
    admin: AdminUser,
    runtime: RuntimeDep,
    _: CsrfChecked,
    name: Annotated[str, Form()] = "",
) -> Response:
    try:
        project = runtime.projects.create(name.strip(), admin.user_id)
    except EngineError as error:
        status, detail, problems = failure(error)
        return render(
            request,
            "admin/projects.html",
            user=admin,
            status_code=status,
            projects=runtime.projects.list_all(),
            error=detail,
            problems=problems,
        )
    return redirect(request, f"/admin/projects/{project.name}", f"Created project '{name}'.")


def _project_page(
    request: Request, ctx: AdminProject, runtime: RuntimeDep, **extra: Any
) -> Response:
    engine = ctx.engine
    workflows = engine.workflows()
    warnings = [w for wf in workflows for w in engine.staffing_warnings(wf.workflow)]
    return render(
        request,
        "admin/project.html",
        user=ctx.user,
        project=ctx.name,
        roles=engine.roles(),
        datasets=engine.datasets(),
        workflows=workflows,
        accounts=[a for a in runtime.accounts.list_all() if a.active],
        staffing=warnings,
        **extra,
    )


@router.get("/projects/{project}")
def project_page(request: Request, ctx: AdminProject, runtime: RuntimeDep) -> Response:
    return _project_page(request, ctx, runtime)


def _project_or_error(
    request: Request, ctx: AdminProject, runtime: RuntimeDep, error: Exception, **extra: Any
) -> Response:
    status, detail, problems = failure(error)
    return _project_page(
        request, ctx, runtime, status_code=status, error=detail, problems=problems, **extra
    )


@router.post("/projects/{project}/roles")
def define_role(
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    _: CsrfChecked,
    role: Annotated[str, Form()] = "",
) -> Response:
    try:
        ctx.engine.define_role(role.strip(), ctx.actor)
    except EngineError as error:
        return _project_or_error(request, ctx, runtime, error)
    return redirect(request, f"/admin/projects/{ctx.name}", f"Defined the role '{role}'.")


def _role_change(
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    role: str,
    user: str,
    reason: str,
    grant: bool,
) -> Response:
    try:
        if grant:
            account = runtime.accounts.require(user)
            if not account.active:
                raise NotAllowed(
                    f"The account '{user}' is deactivated, so it cannot be given a role."
                )
            warnings = ctx.engine.grant_role(user, role, ctx.actor, reason)
        else:
            warnings = ctx.engine.revoke_role(user, role, ctx.actor, reason)
    except EngineError as error:
        return _project_or_error(request, ctx, runtime, error)
    verb = "Gave" if grant else "Took"
    text = f"{verb} the role '{role}' {'to' if grant else 'from'} {user}."
    if warnings:
        text += " Warning: " + " ".join(warnings)
    return redirect(request, f"/admin/projects/{ctx.name}", text, "warn" if warnings else "ok")


@router.post("/projects/{project}/roles/{role}/grant")
def grant_role(
    role: str,
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    _: CsrfChecked,
    user: Annotated[str, Form()] = "",
    reason: Reason = "",
) -> Response:
    return _role_change(request, ctx, runtime, role, user.strip(), reason.strip(), grant=True)


@router.post("/projects/{project}/roles/{role}/revoke")
def revoke_role(
    role: str,
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    _: CsrfChecked,
    user: Annotated[str, Form()] = "",
    reason: Reason = "",
) -> Response:
    return _role_change(request, ctx, runtime, role, user.strip(), reason.strip(), grant=False)


# --- datasets -----------------------------------------------------------------------------------


@router.post("/projects/{project}/datasets")
def create_dataset(
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    _: CsrfChecked,
    name: Annotated[str, Form()] = "",
) -> Response:
    try:
        ctx.engine.create_dataset(name.strip(), ctx.actor)
    except EngineError as error:
        return _project_or_error(request, ctx, runtime, error)
    return redirect(
        request, f"/admin/projects/{ctx.name}/datasets/{name.strip()}", f"Created dataset '{name}'."
    )


def _dataset_page(
    request: Request, ctx: AdminProject, dataset: str, offset: int = 0, **extra: Any
) -> Response:
    page = ctx.engine.items(dataset, offset, 100)
    return render(
        request,
        "admin/dataset.html",
        user=ctx.user,
        project=ctx.name,
        dataset=ctx.engine.dataset(dataset),
        page=page,
        **extra,
    )


@router.get("/projects/{project}/datasets/{dataset}")
def dataset_page(dataset: str, request: Request, ctx: AdminProject, offset: int = 0) -> Response:
    return _dataset_page(request, ctx, dataset, max(0, offset))


def parse_items(text: str) -> list[dict[str, Any]]:
    """Items pasted as JSON (a list of objects, or ``{"items": [...]}``) or as CSV with a
    header row (``item``, ``uri``, optional ``content_hash`` and ``properties`` as JSON;
    any other column becomes a text property)."""
    text = text.strip()
    if not text:
        raise Unprocessable("Paste the items to import, as JSON or CSV.")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as error:
            raise Unprocessable(f"The pasted text is not valid JSON: {error}.") from None
        if isinstance(data, dict):
            data = data.get("items")
        if not isinstance(data, list):
            raise Unprocessable(
                "The JSON must be a list of items, or an object with an `items` list."
            )
        return [row if isinstance(row, dict) else {"item": row} for row in data]
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        row: dict[str, Any] = {}
        properties: dict[str, Any] = {}
        for key, value in raw.items():
            if key is None:
                continue
            key = key.strip()
            value = (value or "").strip()
            if key in ("item", "uri"):
                row[key] = value
            elif key == "content_hash":
                row[key] = value or None
            elif key == "properties":
                if value:
                    try:
                        properties.update(json.loads(value))
                    except ValueError:
                        raise Unprocessable(
                            f"The `properties` of item '{raw.get('item')}' are not valid JSON."
                        ) from None
            elif value:
                properties[key] = value
        row["properties"] = properties
        rows.append(row)
    if not rows:
        raise Unprocessable("The CSV has no rows after its header (`item,uri[,content_hash]`).")
    return rows


@router.post("/projects/{project}/datasets/{dataset}/items")
def import_items(
    dataset: str,
    request: Request,
    ctx: AdminProject,
    runtime: RuntimeDep,
    _: CsrfChecked,
    items: Annotated[str, Form()] = "",
) -> Response:
    try:
        rows = parse_items(items)
        most = getattr(runtime.settings, "max_import_items", 10_000)
        if len(rows) > most:
            raise Unprocessable(f"Import at most {most} items at a time.")
        problems: list[str] = []
        ready: list[NewItem] = []
        for number, row in enumerate(rows, start=1):
            try:
                entry = ItemIn.model_validate(row)
            except ValidationError as error:
                for item in error.errors():
                    where = ".".join(str(part) for part in item["loc"])
                    problems.append(f"Item {number} ({where}): {item['msg']}.")
                continue
            try:
                digest = entry.content_hash or content_hash(
                    entry.item, entry.uri, runtime.settings.media_root
                )
            except CannotHash as error:
                problems.append(str(error))
                continue
            ready.append(
                NewItem(
                    item=entry.item,
                    uri=entry.uri,
                    content_hash=digest.lower(),
                    properties=entry.properties,
                )
            )
        if problems:
            raise Unprocessable(
                f"No items were imported: {len(problems)} of {len(rows)} have problems.", problems
            )
        imported = ctx.engine.import_items(dataset, ready, ctx.actor)
    except (EngineError, ApiError) as error:
        status, detail, problems = failure(error)
        return _dataset_page(
            request, ctx, dataset, status_code=status, error=detail, problems=problems, pasted=items
        )
    return redirect(
        request,
        f"/admin/projects/{ctx.name}/datasets/{dataset}",
        f"Imported {len(imported)} item{'' if len(imported) == 1 else 's'}.",
    )


# --- workflows ----------------------------------------------------------------------------------


def _report_context(report: ConfigReport) -> dict[str, Any]:
    config = report.config
    return {
        "report": report,
        "resolved": resolved_yaml(config) if config else None,
        "diagram": to_mermaid(config) if config else None,
    }


@router.get("/projects/{project}/workflows/new")
def new_workflow(request: Request, ctx: AdminProject) -> Response:
    return render(request, "admin/workflow_new.html", user=ctx.user, project=ctx.name, config="")


@router.post("/projects/{project}/workflows/validate")
def validate_workflow(
    request: Request, ctx: AdminProject, _: CsrfChecked, config: Annotated[str, Form()] = ""
) -> Response:
    """The live check: a fragment with the report, the resolved YAML and the diagram."""
    if not config.strip():
        return render(request, "partials/validation.html", user=ctx.user, report=None)
    report = validate_config(config, ctx.engine.validation_context())
    return render(request, "partials/validation.html", user=ctx.user, **_report_context(report))


@router.post("/projects/{project}/workflows")
def start_workflow(
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    config: Annotated[str, Form()] = "",
    seed: Annotated[str, Form()] = "",
) -> Response:
    report = validate_config(config, ctx.engine.validation_context())
    error: str | None = None
    problems: list[str] = []
    status = 422
    if report.config is None:
        error = report.render().splitlines()[0]
        problems = [str(problem) for problem in report.errors]
    else:
        try:
            chosen = int(seed.strip()) if seed.strip() else secrets.randbits(63)
            warnings = ctx.engine.start_workflow(report.config, chosen, ctx.actor)
        except ValueError:
            error = "The seed must be a whole number from 0 to 2**63 - 1, or left empty."
        except EngineError as engine_error:
            status, error, problems = failure(engine_error)
        else:
            text = f"Started workflow '{report.config.workflow}' with seed {chosen}."
            if warnings:
                text += " Warning: " + " ".join(warnings)
            return redirect(
                request,
                f"/admin/projects/{ctx.name}/workflows/{report.config.workflow}",
                text,
                "warn" if warnings else "ok",
            )
    return render(
        request,
        "admin/workflow_new.html",
        user=ctx.user,
        status_code=status,
        project=ctx.name,
        config=config,
        seed=seed,
        error=error,
        problems=problems,
        **_report_context(report),
    )


def _workflow_page(request: Request, ctx: AdminProject, workflow: str, **extra: Any) -> Response:
    engine = ctx.engine
    detail = engine.workflow(workflow)
    return render(
        request,
        "admin/workflow.html",
        user=ctx.user,
        project=ctx.name,
        workflow=detail,
        stages_by_name={stage.name: stage for stage in detail.stages},
        resolved=resolved_yaml(detail.config),
        diagram=to_mermaid(detail.config),
        versions=engine.config_versions(workflow),
        staffing=engine.staffing_warnings(workflow),
        api_base=f"/projects/{ctx.name}/workflows/{workflow}",
        **extra,
    )


@router.get("/projects/{project}/workflows/{workflow}")
def workflow_page(workflow: str, request: Request, ctx: AdminProject) -> Response:
    return _workflow_page(request, ctx, workflow)


def _workflow_error(
    request: Request, ctx: AdminProject, workflow: str, error: Exception, **extra: Any
) -> Response:
    status, detail, problems = failure(error)
    return _workflow_page(
        request, ctx, workflow, status_code=status, error=detail, problems=problems, **extra
    )


def _done(request: Request, ctx: AdminProject, workflow: str, text: str) -> Response:
    return redirect(request, f"/admin/projects/{ctx.name}/workflows/{workflow}", text)


@router.post("/projects/{project}/workflows/{workflow}/pause")
def pause_workflow(
    workflow: str, request: Request, ctx: AdminProject, _: CsrfChecked, reason: Reason = ""
) -> Response:
    try:
        ctx.engine.pause_workflow(workflow, ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    return _done(
        request, ctx, workflow, f"Paused workflow '{workflow}': claims stop until it is resumed."
    )


@router.post("/projects/{project}/workflows/{workflow}/resume")
def resume_workflow(
    workflow: str, request: Request, ctx: AdminProject, _: CsrfChecked, reason: Reason = ""
) -> Response:
    try:
        ctx.engine.resume_workflow(workflow, ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    return _done(request, ctx, workflow, f"Resumed workflow '{workflow}'.")


@router.post("/projects/{project}/workflows/{workflow}/stages/{stage}/pause")
def pause_stage(
    workflow: str,
    stage: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    reason: Reason = "",
) -> Response:
    try:
        ctx.engine.pause_stage(workflow, stage, ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    return _done(request, ctx, workflow, f"Paused stage '{stage}'.")


@router.post("/projects/{project}/workflows/{workflow}/stages/{stage}/resume")
def resume_stage(
    workflow: str,
    stage: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    reason: Reason = "",
) -> Response:
    try:
        ctx.engine.resume_stage(workflow, stage, ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    return _done(request, ctx, workflow, f"Resumed stage '{stage}'.")


@router.post("/projects/{project}/workflows/{workflow}/stages/{stage}/cancel-open-slots")
def cancel_open_slots(
    workflow: str,
    stage: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    reason: Reason = "",
) -> Response:
    try:
        cancelled = ctx.engine.cancel_open_slots(workflow, stage, ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    count = len(cancelled)
    return _done(
        request,
        ctx,
        workflow,
        f"Cancelled {count} open slot{'' if count == 1 else 's'} of '{stage}'.",
    )


@router.post("/projects/{project}/workflows/{workflow}/items/exclude")
def exclude_item(
    workflow: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    item: Annotated[str, Form()] = "",
    reason: Reason = "",
) -> Response:
    try:
        ctx.engine.exclude_item(workflow, item.strip(), ctx.actor, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error)
    return _done(request, ctx, workflow, f"Excluded item '{item.strip()}' from '{workflow}'.")


@router.get("/projects/{project}/workflows/{workflow}/explain")
def explain_item(workflow: str, request: Request, ctx: AdminProject, item: str = "") -> Response:
    if not item.strip():
        return render(
            request,
            "admin/explain.html",
            user=ctx.user,
            project=ctx.name,
            workflow=workflow,
            item="",
            explanation=None,
        )
    explanation = ctx.engine.explain(workflow, item.strip())
    return render(
        request,
        "admin/explain.html",
        user=ctx.user,
        project=ctx.name,
        workflow=workflow,
        item=item.strip(),
        explanation=explanation,
        text=explanation.render(),
    )


@router.post("/projects/{project}/workflows/{workflow}/preview-edit")
def preview_edit(
    workflow: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    config: Annotated[str, Form()] = "",
) -> Response:
    """The live preview of an edit: a fragment with what it changes and what it touches."""
    if not config.strip():
        return render(request, "partials/preview.html", user=ctx.user, preview=None)
    try:
        preview = ctx.engine.preview_edit(workflow, config)
    except EngineError as error:
        status, detail, problems = failure(error)
        return render(
            request,
            "partials/problem.html",
            user=ctx.user,
            status_code=status,
            status=status,
            detail=detail,
            problems=problems,
        )
    return render(
        request, "partials/preview.html", user=ctx.user, preview=preview, text=preview.render()
    )


@router.post("/projects/{project}/workflows/{workflow}/edits")
def activate_edit(
    workflow: str,
    request: Request,
    ctx: AdminProject,
    _: CsrfChecked,
    config: Annotated[str, Form()] = "",
    base_version: Annotated[str, Form()] = "",
    reason: Reason = "",
) -> Response:
    try:
        base = int(base_version)
    except ValueError:
        return _workflow_page(
            request,
            ctx,
            workflow,
            status_code=422,
            error="The base version must be the version number the edit was previewed against.",
            problems=[],
            edit_config=config,
        )
    try:
        warnings = ctx.engine.activate_edit(workflow, config, ctx.actor, base, reason.strip())
    except EngineError as error:
        return _workflow_error(request, ctx, workflow, error, edit_config=config)
    text = f"Activated config version {base + 1} of '{workflow}'."
    if warnings:
        text += " Warning: " + " ".join(warnings)
    return redirect(
        request,
        f"/admin/projects/{ctx.name}/workflows/{workflow}",
        text,
        "warn" if warnings else "ok",
    )
