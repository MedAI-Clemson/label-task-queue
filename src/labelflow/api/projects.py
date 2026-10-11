"""The list of projects this deployment hosts.

A project is a name; its roles, datasets and workflows live in the store under that name
and are reached through the project's engine (``Runtime.engine_for``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from labelflow.api import db
from labelflow.engine import AlreadyExists, InvalidRequest, NotFound, utc_now
from labelflow.engine.config.models import NAME_PATTERN


@dataclass(frozen=True)
class Project:
    name: str
    created_by: str | None
    created_at: datetime


def unknown_project(name: str) -> NotFound:
    """The one answer for a project that does not exist or that the caller may not see."""
    return NotFound(f"There is no project named '{name}'.")


class Projects:
    """Create and look up projects."""

    def __init__(self, engine: sa.Engine, clock: Callable[[], datetime] = utc_now):
        self.engine = engine
        self.clock = clock
        self._projects = db.table("projects")

    def create(self, name: str, created_by: str | None) -> Project:
        if not re.fullmatch(NAME_PATTERN, name):
            raise InvalidRequest(
                f"'{name}' is not a valid project name: use letters, digits and underscores, "
                "starting with a letter."
            )
        created = self.clock()
        with db.writing(self.engine) as connection:
            if self._exists(connection, name):
                raise AlreadyExists(f"A project named '{name}' already exists.")
            connection.execute(
                sa.insert(self._projects).values(
                    name=name, created_by=created_by, created_at=db.to_text(created)
                )
            )
        return Project(name, created_by, created)

    def require(self, name: str) -> Project:
        with db.reading(self.engine) as connection:
            row = (
                connection.execute(sa.select(self._projects).where(self._projects.c.name == name))
                .mappings()
                .first()
            )
        if row is None:
            raise unknown_project(name)
        return _project(row)

    def list_all(self) -> list[Project]:
        with db.reading(self.engine) as connection:
            rows = connection.execute(sa.select(self._projects).order_by(self._projects.c.name))
            return [_project(row) for row in rows.mappings()]

    def _exists(self, connection: sa.Connection, name: str) -> bool:
        query = sa.select(self._projects.c.name).where(self._projects.c.name == name)
        return connection.execute(query).first() is not None


def _project(row: sa.RowMapping) -> Project:
    return Project(row["name"], row["created_by"], db.from_text(row["created_at"]))
