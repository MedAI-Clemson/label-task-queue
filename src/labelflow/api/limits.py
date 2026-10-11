"""Size limits: the request body as a whole, and the JSON values that can grow (a label, an
item's properties). The limits are settings (see ``settings``); each refusal says the limit.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from labelflow.api.errors import TooLarge

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


def as_size(byte_count: int) -> str:
    """A size in plain words: ``3 KB``, ``20 MB``."""
    if byte_count >= 1024 * 1024:
        return f"{byte_count / (1024 * 1024):g} MB"
    return f"{byte_count / 1024:g} KB"


def json_size(value: Any) -> int:
    """How many bytes the value takes as compact JSON."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def check_size(value: Any, limit: int, what: str) -> None:
    """Refuse (413) a label or properties bigger than ``limit`` bytes as JSON."""
    size = json_size(value)
    if size > limit:
        raise TooLarge(
            f"{what} is too large: {as_size(size)} as JSON, where the limit is {as_size(limit)}."
        )


class BodyLimit:
    """Refuse (413) any request whose body is bigger than ``limit`` bytes.

    A body announced too big by ``Content-Length`` is refused before it is read. One that
    arrives in pieces is refused as soon as the pieces add up to more than the limit.
    """

    def __init__(self, app: App, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.limit:
            response = JSONResponse(
                {"detail": self._message(int(declared)), "problems": []}, status_code=413
            )
            return await response(scope, receive, send)
        received = 0

        async def receive_within_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise TooLarge(self._message())
            return message

        await self.app(scope, receive_within_limit, send)

    def _message(self, size: int | None = None) -> str:
        actual = f" ({as_size(size)})" if size else ""
        return (
            f"The request is too large{actual}: the server accepts at most "
            f"{as_size(self.limit)}. Send fewer items at a time, or smaller ones."
        )
