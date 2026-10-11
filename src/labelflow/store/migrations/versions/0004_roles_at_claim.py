"""Record the roles held at the claim on slots and labels.

A held claim survives a role change, so the roles a labeler held when they submitted (the
``roles`` column of ``labels``) can differ from the roles that made them eligible, held at
the claim. ``slots.claimed_roles`` keeps the latter while a slot is claimed or filled (NULL
while it is open or once it is cancelled), and ``labels.roles_at_claim`` copies it onto the
label. Both are derived columns, filled by replaying ``slot_claimed`` events.

Existing rows are filled from the event log here, in SQL: a claimed or filled slot takes the
roles of its latest ``slot_claimed`` event, and each label takes its slot's roles. That is
what a replay through the reducer gives, so ``verify_derived`` finds no difference after the
upgrade. Both columns are nullable: SQLite cannot add a NOT NULL column without a default,
and None is what the store writes there for an open or cancelled slot anyway.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("slots", sa.Column("claimed_roles", sa.Text(), nullable=True))
    op.add_column("labels", sa.Column("roles_at_claim", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE slots SET claimed_roles = (
            SELECT json_extract(e.event, '$.roles') FROM events AS e
            WHERE e.project = slots.project
              AND e.type = 'slot_claimed'
              AND json_extract(e.event, '$.slot') = slots.slot
            ORDER BY e.seq DESC LIMIT 1)
        WHERE claimed_by IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE labels SET roles_at_claim = (
            SELECT s.claimed_roles FROM slots AS s
            WHERE s.project = labels.project AND s.slot = labels.slot)
        """
    )


def downgrade() -> None:
    op.drop_column("labels", "roles_at_claim")
    op.drop_column("slots", "claimed_roles")
