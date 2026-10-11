"""The server-rendered interface: a small app of its own, mounted under ``/ui``.

Pages are Jinja2 templates; forms post back to this app; the only JavaScript beyond the
vendored HTMX is the mask tool on the labeler page (``static/label.js`` on top of
``static/maskcore.js``). The pages share the API's runtime (accounts, logins, engines) and
its login cookie, and every page is reached by its address under the mount.

Why a separate app rather than more API routes: the UI answers HTML, redirects to the login
page when nobody is logged in, and renders errors as pages (or as fragments for HTMX
requests). Keeping it apart also keeps the OpenAPI document to the API.

Forms are protected twice: the login cookie is ``SameSite=lax``, so a cross-site POST does
not carry it, and every form also carries a token (``csrf_token``) that is an HMAC of the
login cookie under a key made when the app starts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from labelflow.api.deps import COOKIE
from labelflow.api.errors import ApiError, Forbidden, NotLoggedIn, status_for
from labelflow.api.identity import Principal
from labelflow.engine import EngineError
from labelflow.labeltask import UnknownLabelTask

HERE = Path(__file__).parent
FLASH_COOKIE = "labelflow_flash"

log = logging.getLogger("labelflow.ui")
templates = Jinja2Templates(directory=HERE / "templates")


# --- helpers used by every page ---------------------------------------------------------------


def root(request: Request) -> str:
    """The path this app is mounted at (``/ui``), to build links with."""
    return request.scope.get("root_path", "").rstrip("/")


def csrf_token(request: Request) -> str:
    """The form token for this login: an HMAC of the login cookie under the app's key."""
    key: bytes = request.app.state.csrf_key
    session = request.cookies.get(COOKIE, "")
    return hmac.new(key, session.encode(), hashlib.sha256).hexdigest()


def check_csrf(request: Request, csrf: Annotated[str, Form()] = "") -> None:
    """Refuse a form post whose token does not match the login cookie."""
    if not hmac.compare_digest(csrf, csrf_token(request)):
        raise Forbidden(
            "This form was not sent from a page of this site, or the page is out of date. "
            "Reload the page and try again."
        )


CsrfChecked = Annotated[None, Depends(check_csrf)]


def wants_fragment(request: Request) -> bool:
    """Whether HTMX sent the request, so a fragment is wanted rather than a whole page."""
    return request.headers.get("HX-Request") == "true"


def render(
    request: Request,
    name: str,
    *,
    user: Principal | None = None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    """Render a page. Every page gets the mount path, the user, the form token and the
    flash message left by the previous request, if any."""
    flash = _read_flash(request)
    response = templates.TemplateResponse(
        request,
        name,
        {
            "root": root(request),
            "user": user,
            "csrf": csrf_token(request),
            "flash": flash,
            **context,
        },
        status_code=status_code,
    )
    if flash is not None:
        response.delete_cookie(FLASH_COOKIE, path=root(request) or "/")
    return response


def redirect(
    request: Request, path: str, flash: str | None = None, kind: str = "ok"
) -> RedirectResponse:
    """Redirect (303) to a path under the mount, leaving a message for the next page."""
    response = RedirectResponse(root(request) + path, status_code=303)
    if flash:
        response.set_cookie(
            FLASH_COOKIE,
            _encode({"kind": kind, "text": flash}),
            path=root(request) or "/",
            httponly=True,
            samesite="lax",
        )
    return response


def _encode(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _read_flash(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or "text" not in data:
        return None
    return {"kind": str(data.get("kind", "ok")), "text": str(data["text"])}


def safe_next(request: Request, target: str | None) -> str:
    """Where to go after logging in: a path on this site, else the home page."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return root(request) + "/"


# --- errors as pages ----------------------------------------------------------------------------


def _problem(request: Request, status: int, detail: str, problems: list[str]) -> Response:
    name = "partials/problem.html" if wants_fragment(request) else "error.html"
    return render(
        request, name, status_code=status, status=status, detail=detail, problems=problems
    )


def _not_logged_in(request: Request, _: Exception) -> Response:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    login = f"{root(request)}/login?next={quote(target, safe='/')}"
    if wants_fragment(request):
        return Response(status_code=401, headers={"HX-Redirect": login})
    return RedirectResponse(login, status_code=303)


def _api_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, ApiError)
    return _problem(request, error.status, error.detail, error.problems)


def _engine_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, EngineError)
    return _problem(request, status_for(error), str(error), list(getattr(error, "problems", ())))


def _unknown_label_task(request: Request, error: Exception) -> Response:
    return _problem(
        request,
        500,
        f"{error} The server does not have this label task installed. Ask whoever runs the "
        "server to restore its ontology file.",
        [],
    )


def _bad_request(request: Request, error: Exception) -> Response:
    assert isinstance(error, RequestValidationError)
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"] if part not in ("body", "query"))
        problems.append(f"{where}: {item['msg']}" if where else str(item["msg"]))
    return _problem(request, 422, "The form is not complete or not valid.", problems)


def _unexpected(request: Request, error: Exception) -> Response:
    log.exception("Unhandled error in the interface", exc_info=error)
    return _problem(
        request, 500, "Something went wrong on the server. The details are in the server log.", []
    )


# --- building and mounting --------------------------------------------------------------------


def build_ui(runtime: Any) -> FastAPI:
    """The interface app, sharing the API's ``runtime``."""
    from labelflow.ui import admin, auth, home, label  # here, to avoid an import cycle

    ui = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    ui.state.runtime = runtime
    ui.state.csrf_key = secrets.token_bytes(32)
    ui.add_exception_handler(NotLoggedIn, _not_logged_in)
    ui.add_exception_handler(ApiError, _api_error)
    ui.add_exception_handler(EngineError, _engine_error)
    ui.add_exception_handler(UnknownLabelTask, _unknown_label_task)
    ui.add_exception_handler(RequestValidationError, _bad_request)
    ui.add_exception_handler(Exception, _unexpected)
    ui.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    for module in (auth, home, label, admin):
        ui.include_router(module.router)
    return ui


def mount_ui(app: FastAPI, path: str = "/ui") -> None:
    """Mount the interface under ``path`` of the API app, and send ``/`` there."""
    app.mount(path, build_ui(app.state.runtime), name="ui")

    def to_ui(_: Request) -> Response:
        return RedirectResponse(f"{path}/", status_code=303)

    app.add_route("/", to_ui, include_in_schema=False)
