"""Deployment-level tables: accounts, login sessions and the list of projects.

These are not project state, so they are ordinary tables and not events. Accounts say who may
log in; the engine only ever sees an account's ``user_id`` (the username). Sessions hold a
login by the SHA-256 of its token, never the token itself. ``projects`` lists the projects
the deployment hosts (each project's own rows carry the project name, see ``schema``).

The tables are declared here, next to the derived tables, so Alembic sees them; the
``0002_accounts`` migration is a written-out copy.
"""

from __future__ import annotations

import sqlalchemy as sa

ACCOUNTS = "accounts"
SESSIONS = "sessions"
PROJECTS = "projects"


def add_deployment_tables(metadata: sa.MetaData) -> None:
    """Add the accounts, sessions and projects tables to ``metadata``."""
    sa.Table(
        ACCOUNTS,
        metadata,
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_accounts"),
    )
    sa.Table(
        SESSIONS,
        metadata,
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{ACCOUNTS}.user_id"], name="fk_sessions_user_id_accounts"
        ),
        sa.Index("ix_sessions_user_id", "user_id"),
    )
    sa.Table(
        PROJECTS,
        metadata,
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_projects"),
    )
