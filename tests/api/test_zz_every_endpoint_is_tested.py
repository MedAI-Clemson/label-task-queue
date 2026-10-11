"""Every endpoint is called by the tests, on its success path and on an error path.

This runs last (its name sorts last) and looks at the calls the other tests made. It is
skipped when only some of the API tests ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

NO_ERROR_PATH = {("GET", "/health"), ("POST", "/auth/logout")}  # they cannot fail


def route_of(app, method, path):
    scope = {"type": "http", "method": method, "path": path}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.matches(scope)[0] == Match.FULL:
            return route
    return None


def test_every_endpoint_is_tested(app, request, recorded_calls):
    here = Path(__file__).parent
    ran = {Path(str(item.fspath)).name for item in request.session.items}
    if not {p.name for p in here.glob("test_*.py")} <= ran:
        pytest.skip("only some of the API tests ran")

    statuses: dict[tuple[str, str], set[int]] = {}
    for method, path, status in recorded_calls:
        route = route_of(app, method, path)
        if route is not None:
            statuses.setdefault((method, route.path), set()).add(status)

    problems = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods):
            seen = statuses.get((method, route.path), set())
            if not any(status < 300 for status in seen):
                problems.append(f"{method} {route.path}: no successful call")
            if (method, route.path) not in NO_ERROR_PATH and not any(s >= 400 for s in seen):
                problems.append(f"{method} {route.path}: no error path")
    assert not problems, "\n".join(problems)
