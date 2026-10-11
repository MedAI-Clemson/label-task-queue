"""What a running API needs: the database, the label tasks, accounts and the clock.

``Runtime`` is built by ``create_app`` and starts on first use, or at startup when the app
runs under a server (so a missing ontology folder or an unmigrated database stops the
server with a clear message instead of failing the first request).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from ltq.api.accounts import Accounts
from ltq.api.identity import Identity, LocalIdentity
from ltq.api.projects import Projects
from ltq.api.settings import Settings
from ltq.engine import Engine, utc_now
from ltq.labeltask import LabelTaskRegistry, registry_from_directory
from ltq.store import SqlStore, connect

REQUIRED_TABLES = ("events", "accounts", "sessions", "projects")


class Runtime:
    """The shared parts of the API. Call ``ready()`` before using the others."""

    def __init__(
        self,
        settings: Settings,
        registry: LabelTaskRegistry | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.settings = settings
        self.clock = clock
        self._registry = registry
        self._lock = threading.Lock()
        self._started = False
        self.db: sa.Engine
        self.accounts: Accounts
        self.identity: Identity
        self.projects: Projects

    @property
    def registry(self) -> LabelTaskRegistry:
        assert self._registry is not None, "call ready() first"
        return self._registry

    def ready(self) -> None:
        """Open the database and load the label tasks (once; later calls do nothing)."""
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            settings = self.settings
            if self._registry is None:
                self._registry = registry_from_directory(settings.label_tasks)
            make_database_folder(settings.database_uri)
            self.db = connect(settings.database_uri, synchronous=settings.sqlite_synchronous)
            try:
                check_migrated(self.db, settings.database_uri)
            except BaseException:
                self.db.dispose()
                raise
            self.accounts = Accounts(self.db, settings.password_cost, self.clock)
            self.identity = LocalIdentity(
                self.db,
                self.accounts,
                settings.session_lifetime,
                self.clock,
                settings.password_cost,
            )
            self.projects = Projects(self.db, self.clock)
            self._started = True

    def close(self) -> None:
        with self._lock:
            if self._started:
                self.db.dispose()
                self._started = False

    def engine_for(self, project: str) -> Engine:
        """The labeling engine of one project."""
        return Engine(SqlStore(self.db, project), self.registry, clock=self.clock)


def make_database_folder(uri: str) -> None:
    url = sa.make_url(uri)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def check_migrated(engine: sa.Engine, uri: str) -> None:
    present = set(sa.inspect(engine).get_table_names())
    if not present.issuperset(REQUIRED_TABLES):
        raise RuntimeError(
            f"The database {uri!r} is not set up (or is out of date). "
            "Run `alembic upgrade head` first, then start the server again."
        )
