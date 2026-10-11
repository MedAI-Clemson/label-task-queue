"""The identity layer: who is asking, as a stable user id.

Everything past this module (the engine, the permission checks) sees only a ``Principal``.
``LocalIdentity`` is the self-hosted way to get one: a username and password checked
against the accounts an admin created, and a login token kept as a session. Another login
method (single sign-on, say) would be another class with the same three methods.

A login token is random and is kept only as its SHA-256, so the database does not hold
anything that can be used to log in. Tokens are looked up on every request.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import sqlalchemy as sa

from labelflow.api import db
from labelflow.api.accounts import Accounts
from labelflow.api.errors import NotLoggedIn
from labelflow.api.passwords import hash_password, verify_password
from labelflow.engine import utc_now


@dataclass(frozen=True)
class Principal:
    """A logged-in person: the stable id the engine sees, and what they may do."""

    user_id: str
    display_name: str
    is_admin: bool


@dataclass(frozen=True)
class Login:
    token: str
    expires_at: datetime
    principal: Principal


class Identity(Protocol):
    """What the rest of the API needs from a login method."""

    def login(self, username: str, password: str) -> Login:
        """Check the credentials and start a session. Raises NotLoggedIn if refused."""
        ...

    def authenticate(self, token: str | None) -> Principal | None:
        """The person a token belongs to, or None if it is unknown, expired or deactivated."""
        ...

    def logout(self, token: str | None) -> None:
        """End the session the token belongs to (nothing happens for an unknown token)."""
        ...


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LocalIdentity:
    """Username and password accounts, with sessions kept in the database."""

    def __init__(
        self,
        engine: sa.Engine,
        accounts: Accounts,
        lifetime: timedelta,
        clock: Callable[[], datetime] = utc_now,
        password_cost: int = 14,
    ):
        self.engine = engine
        self.accounts = accounts
        self.lifetime = lifetime
        self.clock = clock
        self._sessions = db.table("sessions")
        self._accounts = db.table("accounts")
        # Checked when the username is unknown, so a wrong username takes as long as a
        # wrong password and a reply does not tell which of the two it was.
        self._decoy = hash_password(secrets.token_urlsafe(8), password_cost)

    def login(self, username: str, password: str) -> Login:
        stored = self.accounts.password_hash(username)
        if not verify_password(password, stored or self._decoy) or stored is None:
            raise NotLoggedIn("Wrong username or password.")
        account = self.accounts.require(username)
        if not account.active:
            raise NotLoggedIn("This account has been deactivated. Ask an administrator.")
        now = self.clock()
        token = secrets.token_urlsafe(32)
        expires = now + self.lifetime
        with db.writing(self.engine) as connection:
            connection.execute(
                sa.delete(self._sessions).where(self._sessions.c.expires_at <= db.to_text(now))
            )
            connection.execute(
                sa.insert(self._sessions).values(
                    token_hash=token_hash(token),
                    user_id=username,
                    created_at=db.to_text(now),
                    expires_at=db.to_text(expires),
                )
            )
        principal = Principal(account.user_id, account.display_name, account.is_admin)
        return Login(token, expires, principal)

    def authenticate(self, token: str | None) -> Principal | None:
        if not token:
            return None
        digest = token_hash(token)
        query = (
            sa.select(
                self._accounts.c.user_id,
                self._accounts.c.display_name,
                self._accounts.c.is_admin,
                self._sessions.c.expires_at,
            )
            .select_from(
                self._sessions.join(
                    self._accounts, self._accounts.c.user_id == self._sessions.c.user_id
                )
            )
            .where(self._sessions.c.token_hash == digest, self._accounts.c.active.is_(True))
        )
        with db.reading(self.engine) as connection:
            row = connection.execute(query).mappings().first()
        if row is None:
            return None
        if db.from_text(row["expires_at"]) <= self.clock():
            self.logout(token)
            return None
        return Principal(row["user_id"], row["display_name"], bool(row["is_admin"]))

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with db.writing(self.engine) as connection:
            connection.execute(
                sa.delete(self._sessions).where(self._sessions.c.token_hash == token_hash(token))
            )
