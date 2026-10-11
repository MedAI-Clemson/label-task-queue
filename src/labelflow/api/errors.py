"""Turning errors into HTTP responses, in one place.

Every error response has the same shape: ``detail`` is the plain-language message, and
``problems`` lists every problem when there are several (an invalid config, a refused edit, a
rejected label, a bad import batch). The engine's errors keep their own wording.

======================================================  ======
Engine error                                            Status
======================================================  ======
something unknown (role, dataset, item, workflow, ...)  404
already exists; edit based on an old version            409
not allowed in the current state (already excluded...)  409
a claim while paused; pausing or resuming twice         409
not eligible; slot not claimed by you                   403
refused edit; invalid label; malformed request          422
======================================================  ======
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from labelflow.engine import (
    AlreadyExists,
    BrokenChain,
    EditRefused,
    EngineError,
    InvalidLabel,
    InvalidRequest,
    InvalidShown,
    NotAllowed,
    NotEligible,
    NotFound,
    Paused,
    SlotNotYours,
    VersionConflict,
)
from labelflow.labeltask import UnknownLabelTask

log = logging.getLogger("labelflow.api")


class ErrorOut(BaseModel):
    """What every error response looks like."""

    detail: str = Field(description="What went wrong, in plain words, and what to do about it.")
    problems: list[str] = Field(
        default=[],
        description="Every problem found, when there is more than one (for example, a config "
        "with several mistakes). Empty when `detail` says it all.",
    )


class ApiError(Exception):
    """A request refused by the web layer itself. ``problems`` lists every problem found."""

    status = 400

    def __init__(self, detail: str, problems: Sequence[str] = ()):
        super().__init__(detail)
        self.detail = detail
        self.problems = list(problems)


class NotLoggedIn(ApiError):
    """No valid login: missing, wrong, expired or deactivated."""

    status = 401


class Forbidden(ApiError):
    """Logged in, but this account may not do this."""

    status = 403


class Unprocessable(ApiError):
    """A request with problems in it (a config with errors, a bad import batch). ``problems``
    lists every one."""

    status = 422


# The first entry that matches a class in the error's inheritance chain wins, so a subclass
# listed here (or inherited) beats its parent class.
_STATUS: dict[type[Exception], int] = {
    VersionConflict: 409,
    EditRefused: 422,
    Paused: 409,  # like NotAllowed: the state forbids it for now, not the person
    NotEligible: 403,
    SlotNotYours: 403,
    NotAllowed: 409,
    NotFound: 404,
    AlreadyExists: 409,
    InvalidRequest: 422,
    InvalidLabel: 422,
    InvalidShown: 422,
    BrokenChain: 500,
    EngineError: 422,
}


def status_for(error: Exception) -> int:
    """The HTTP status for an engine error."""
    for kind in type(error).__mro__:
        if kind in _STATUS:
            return _STATUS[kind]
    return 500


def _body(detail: str, problems: Sequence[str] = ()) -> dict[str, Any]:
    return {"detail": detail, "problems": list(problems)}


def _reply(status: int, detail: str, problems: Sequence[str] = ()) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(_body(detail, problems), status_code=status, headers=headers)


def _engine_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, EngineError)
    return _reply(status_for(error), str(error), getattr(error, "problems", ()))


def _api_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)
    return _reply(error.status, error.detail, error.problems)


def _unknown_label_task(_: Request, error: Exception) -> JSONResponse:
    return _reply(
        500,
        f"{error} The server does not have this label task installed, so work on workflows "
        "that use it is not possible. Ask whoever runs the server to restore its ontology file.",
    )


def _bad_request(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"] if part not in ("body", "query"))
        message = str(item["msg"])
        problems.append(f"{where}: {message}" if where else message)
    if len(problems) == 1:
        return _reply(422, f"The request is not valid. {problems[0]}", problems)
    return _reply(422, f"The request is not valid: {len(problems)} problems.", problems)


def _unexpected(_: Request, error: Exception) -> JSONResponse:
    log.exception("Unhandled error", exc_info=error)
    return _reply(500, "Something went wrong on the server. The details are in the server log.")


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(EngineError, _engine_error)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(UnknownLabelTask, _unknown_label_task)
    app.add_exception_handler(RequestValidationError, _bad_request)
    app.add_exception_handler(Exception, _unexpected)


_DESCRIPTIONS = {
    401: "You are not logged in, or the login is wrong or has expired.",
    403: "This account may not do this (or the slot is not yours to work on).",
    404: "Nothing with that name exists.",
    409: "It already exists, the current state does not allow it (such as a claim while the "
    "stage is paused), or the request was based on an old config version.",
    422: "The request is not valid. `problems` lists everything that is wrong.",
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI descriptions of the error responses an endpoint can give."""
    return {code: {"model": ErrorOut, "description": _DESCRIPTIONS[code]} for code in codes}
