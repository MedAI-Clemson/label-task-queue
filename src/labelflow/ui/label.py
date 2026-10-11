"""The labeler's pages: a workflow, the next slot, the labeling page, the image, release.

The labeling page embeds the work (slot, packet and ``shown``) and the ontology as JSON and
hands them to ``static/label.js``, which draws the image, the earlier labels as layers and
the masks being edited. Submitting is a JSON post from the page to the API's own submit
endpoint, with the packet's ``shown`` reported as given: the page offers every earlier
label of the packet as a layer, and a blind stage has none.

The image route serves ``file://`` images and paths inside ``LABELFLOW_MEDIA_ROOT``, and only
to the person who holds the slot. Images at an ``http(s)`` address are loaded by the browser
straight from there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from labelflow.api.deps import RuntimeDep, UserProject
from labelflow.api.errors import Forbidden
from labelflow.engine import NotFound
from labelflow.labeltask import LabelTask, OntologyLabelTask
from labelflow.ui.app import CsrfChecked, redirect, render
from labelflow.ui.home import waiting_for

router = APIRouter(prefix="/projects/{project}/workflows/{workflow}")


def _check_role(ctx: UserProject, workflow: str) -> set[str]:
    """The roles of the workflow this person holds; admins may look without any."""
    detail = ctx.engine.workflow(workflow)  # 404 for an unknown workflow
    used = {role for stage in detail.stages for role in stage.roles}
    mine = ctx.engine.user_roles(ctx.actor) & used
    if not mine and not ctx.user.is_admin:
        raise Forbidden("You do not hold a role in this workflow.")
    return mine


@router.get("")
def workflow_page(workflow: str, request: Request, ctx: UserProject) -> Response:
    mine = _check_role(ctx, workflow)
    detail = ctx.engine.workflow(workflow)
    waiting, held = waiting_for(ctx.engine, workflow, ctx.actor)
    return render(
        request,
        "workflow.html",
        user=ctx.user,
        project=ctx.name,
        workflow=detail,
        my_roles=sorted(mine),
        waiting=waiting,
        held=held,
    )


@router.post("/next")
def next_work(workflow: str, request: Request, ctx: UserProject, _: CsrfChecked) -> Response:
    slot = ctx.engine.request_work(workflow, ctx.actor)
    if slot is None:
        detail = ctx.engine.workflow(workflow)
        mine = ctx.engine.user_roles(ctx.actor) & {
            role for stage in detail.stages for role in stage.roles
        }
        return render(
            request,
            "nowork.html",
            user=ctx.user,
            project=ctx.name,
            workflow=detail,
            my_roles=sorted(mine),
        )
    return redirect(request, f"/projects/{ctx.name}/workflows/{workflow}/slots/{slot.slot}")


def ontology_json(task: LabelTask) -> dict[str, Any] | None:
    """The ontology the page builds its controls from; None for a label task without one
    (the page then takes the label as JSON text)."""
    if isinstance(task, OntologyLabelTask):
        return task.ontology.model_dump(mode="json")
    return None


@router.get("/slots/{slot}")
def slot_page(workflow: str, slot: int, request: Request, ctx: UserProject) -> Response:
    engine = ctx.engine
    packet = engine.work_packet(workflow, slot, ctx.actor)
    held = engine.slot(workflow, slot)
    task = engine.label_tasks.get(engine.workflow(workflow).label_task)
    work = {
        "slot": held.model_dump(mode="json"),
        "packet": packet.model_dump(mode="json"),
        "shown": packet.shown().model_dump(mode="json"),
    }
    base = f"/projects/{ctx.name}/workflows/{workflow}/slots/{slot}"
    page = {
        "api_base": base,
        "image_url": _image_url(packet.uri, request.scope.get("root_path", "") + base),
    }
    return render(
        request,
        "label.html",
        user=ctx.user,
        project=ctx.name,
        workflow=workflow,
        slot=held,
        packet=packet,
        work_json=script_json(work),
        ontology_json=script_json(ontology_json(task)),
        page_json=script_json(page),
        task=task,
    )


def script_json(data: Any) -> str:
    """JSON for a ``<script type="application/json">`` block: ``<`` is escaped so that no
    value can close the block, and the result is marked safe for the template."""
    return json.dumps(data).replace("<", "\\u003c")


def _image_url(uri: str, ui_base: str) -> str:
    scheme = urlparse(uri).scheme
    if scheme in ("http", "https"):
        return uri
    return f"{ui_base}/image"


@router.get("/slots/{slot}/image")
def slot_image(workflow: str, slot: int, ctx: UserProject, runtime: RuntimeDep) -> Response:
    packet = ctx.engine.work_packet(workflow, slot, ctx.actor)  # 403 unless the slot is yours
    scheme = urlparse(packet.uri).scheme
    if scheme in ("http", "https"):
        return RedirectResponse(packet.uri, status_code=307)
    path = media_file(packet.uri, runtime.settings.media_root)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


def media_file(uri: str, media_root: Path | None) -> Path:
    """The file an item's uri names, when the server may serve it: a ``file://`` address or
    a plain path, which must lie inside the media folder (when one is set)."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise NotFound(f"The image '{uri}' is on another computer; it cannot be shown.")
        path = Path(url2pathname(parsed.path))
    elif not parsed.scheme:
        path = Path(uri)
        if not path.is_absolute():
            if media_root is None:
                raise NotFound(
                    f"The image '{uri}' is a relative path, and the server has no media folder."
                )
            path = media_root / path
    else:
        raise NotFound(f"The image '{uri}' is not a file the server can serve.")
    if media_root is None:
        raise Forbidden(
            "The server has no media folder (LABELFLOW_MEDIA_ROOT), so it serves no local images."
        )
    resolved = path.resolve()
    if not resolved.is_relative_to(media_root.resolve()):
        raise Forbidden(f"The image '{uri}' is outside the server's media folder.")
    if not resolved.is_file():
        raise NotFound(f"No file was found at the image's address '{uri}'.")
    return resolved


@router.post("/slots/{slot}/release")
def release(
    workflow: str,
    slot: int,
    request: Request,
    ctx: UserProject,
    _: CsrfChecked,
    reason: Annotated[str, Form()] = "",
) -> Response:
    if not reason.strip():
        return render(
            request,
            "error.html",
            user=ctx.user,
            status_code=422,
            status=422,
            detail="Say why you are giving the work back; the reason is kept in the record.",
            problems=[],
        )
    ctx.engine.release(workflow, slot, ctx.actor, reason.strip())
    return redirect(
        request,
        f"/projects/{ctx.name}/workflows/{workflow}",
        f"You gave slot {slot} back. It will be offered to you again after your other work.",
    )
