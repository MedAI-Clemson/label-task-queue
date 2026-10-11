"""Command-line administration: make the first admin account.

    python -m labelflow.api.admin create-admin <username>

The password is read from ``LABELFLOW_ADMIN_PASSWORD`` if set, otherwise asked for (twice, hidden).
The database must already be set up (``alembic upgrade head``). Other accounts are created
by an admin through the API.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence

import sqlalchemy as sa

from labelflow.api.accounts import Accounts
from labelflow.api.runtime import check_migrated, make_database_folder
from labelflow.api.settings import Settings
from labelflow.engine import EngineError
from labelflow.store import connect


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m labelflow.api.admin", description="Administer the label-task-queue server."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="create an administrator account")
    create.add_argument("username", help="lowercase letters, digits, '_', '.' or '-'")
    create.add_argument("--display-name", default="", help="the name shown to people")
    return parser.parse_args(argv)


def ask_password(prompt: Callable[[str], str] = getpass.getpass) -> str:
    try:
        password = prompt("Password: ")
        again = prompt("Password again: ")
    except EOFError:
        raise ValueError(
            "No password was given. Set LABELFLOW_ADMIN_PASSWORD, or run this in a terminal."
        ) from None
    if again != password:
        raise ValueError("The two passwords are not the same.")
    return password


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    """Run a command; the exit status is 0 when done and 1 when it could not be."""
    args = parse(argv)
    env = os.environ if env is None else env
    try:
        settings = Settings.from_env(env)
        password = env.get("LABELFLOW_ADMIN_PASSWORD") or ask_password(prompt)
        make_database_folder(settings.database_uri)
        database = connect(settings.database_uri, synchronous=settings.sqlite_synchronous)
        try:
            check_migrated(database, settings.database_uri)
            accounts = Accounts(database, settings.password_cost)
            account = accounts.create(
                args.username, password, args.display_name, is_admin=True, created_by=None
            )
        finally:
            database.dispose()
    except (ValueError, EngineError, RuntimeError, sa.exc.SQLAlchemyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Created the administrator account '{account.user_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
