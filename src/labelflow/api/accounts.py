"""Accounts: who may log in. An admin creates every account; there is no self-signup.

An account's ``user_id`` is its username. It is the only identity the engine ever sees: the
actor of every event, and the person in every role grant. The ``accounts`` table is a
deployment table, not project state (see ``labelflow.store.deployment``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from labelflow.api import db
from labelflow.api.passwords import hash_password
from labelflow.engine import AlreadyExists, InvalidRequest, NotFound, utc_now

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256


@dataclass(frozen=True)
class Account:
    user_id: str
    display_name: str
    is_admin: bool
    active: bool
    created_by: str | None
    created_at: datetime


def check_username(username: str) -> None:
    if not re.fullmatch(USERNAME_PATTERN, username):
        raise InvalidRequest(
            f"'{username}' is not a valid username: use 1 to 64 lowercase letters, digits, "
            "'_', '.' or '-', starting with a letter or digit."
        )


def check_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidRequest(
            f"The password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long."
        )


class Accounts:
    """Create, look up, list, deactivate accounts and reset passwords."""

    def __init__(
        self, engine: sa.Engine, password_cost: int = 14, clock: Callable[[], datetime] = utc_now
    ):
        self.engine = engine
        self.password_cost = password_cost
        self.clock = clock
        self._accounts = db.table("accounts")
        self._sessions = db.table("sessions")

    def create(
        self,
        username: str,
        password: str,
        display_name: str = "",
        is_admin: bool = False,
        created_by: str | None = None,
    ) -> Account:
        check_username(username)
        check_password(password)
        row = {
            "user_id": username,
            "display_name": display_name.strip() or username,
            "password_hash": hash_password(password, self.password_cost),
            "is_admin": is_admin,
            "active": True,
            "created_by": created_by,
            "created_at": db.to_text(self.clock()),
        }
        with db.writing(self.engine) as connection:
            if self._row(connection, username) is not None:
                raise AlreadyExists(f"An account named '{username}' already exists.")
            connection.execute(sa.insert(self._accounts).values(row))
        return _account(row)

    def get(self, username: str) -> Account | None:
        with db.reading(self.engine) as connection:
            row = self._row(connection, username)
        return None if row is None else _account(row)

    def password_hash(self, username: str) -> str | None:
        """The stored hash, for the login check (never sent anywhere)."""
        with db.reading(self.engine) as connection:
            row = self._row(connection, username)
        return None if row is None else row["password_hash"]

    def require(self, username: str) -> Account:
        account = self.get(username)
        if account is None:
            raise NotFound(f"There is no account named '{username}'.")
        return account

    def list_all(self) -> list[Account]:
        with db.reading(self.engine) as connection:
            rows = connection.execute(sa.select(self._accounts).order_by(self._accounts.c.user_id))
            return [_account(row) for row in rows.mappings()]

    def deactivate(self, username: str) -> Account:
        """Stop the account from logging in and end its logins. Its history stays."""
        with db.writing(self.engine) as connection:
            if self._row(connection, username) is None:
                raise NotFound(f"There is no account named '{username}'.")
            connection.execute(
                sa.update(self._accounts)
                .where(self._accounts.c.user_id == username)
                .values(active=False)
            )
            connection.execute(
                sa.delete(self._sessions).where(self._sessions.c.user_id == username)
            )
            row = self._row(connection, username)
        return _account(row)

    def set_password(self, username: str, password: str) -> None:
        """Replace the password and end the account's logins."""
        check_password(password)
        new_hash = hash_password(password, self.password_cost)
        with db.writing(self.engine) as connection:
            if self._row(connection, username) is None:
                raise NotFound(f"There is no account named '{username}'.")
            connection.execute(
                sa.update(self._accounts)
                .where(self._accounts.c.user_id == username)
                .values(password_hash=new_hash)
            )
            connection.execute(
                sa.delete(self._sessions).where(self._sessions.c.user_id == username)
            )

    def _row(self, connection: sa.Connection, username: str) -> sa.RowMapping | dict | None:
        query = sa.select(self._accounts).where(self._accounts.c.user_id == username)
        return connection.execute(query).mappings().first()


def _account(row: sa.RowMapping | dict) -> Account:
    created = row["created_at"]
    return Account(
        user_id=row["user_id"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        active=bool(row["active"]),
        created_by=row["created_by"],
        created_at=db.from_text(created) if isinstance(created, str) else created,
    )
