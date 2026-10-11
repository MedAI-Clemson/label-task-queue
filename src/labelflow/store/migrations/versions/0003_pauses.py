"""Add the pause state of workflows and stages.

``workflow_pauses`` and ``stage_pauses`` hold one row per workflow or stage that was ever
paused: whether it is paused now, and who paused or resumed it last, when and why. They are
derived tables like the others, filled by replaying ``workflow_paused``, ``workflow_resumed``,
``stage_paused`` and ``stage_resumed`` events. A database upgraded to this revision has no
such events yet, so both tables start empty, which is the right state: nothing is paused.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_pauses",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("by", sa.Text(), nullable=True),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", name="pk_workflow_pauses"),
    )
    op.create_table(
        "stage_pauses",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("by", sa.Text(), nullable=True),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", "stage", name="pk_stage_pauses"),
    )


def downgrade() -> None:
    op.drop_table("stage_pauses")
    op.drop_table("workflow_pauses")
