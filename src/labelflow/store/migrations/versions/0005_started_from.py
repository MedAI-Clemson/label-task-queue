"""Record the earlier label a reviewer started from on the label.

A reviewer in a stage that shows earlier labels may copy one of them into the editor
("Start from this layer") instead of drawing every structure again. The ``label_submitted``
event records that label as ``shown.started_from``, and ``labels.started_from`` copies it, so
the labels export can show who started from what.

Existing rows are filled from the event log here, in SQL, exactly as a replay through the
reducer would (events logged before this revision have no ``started_from``, so their labels
get NULL), so ``verify_derived`` finds no difference after the upgrade. The column is
nullable: most labels start from nothing.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("labels", sa.Column("started_from", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE labels SET started_from = (
            SELECT json_extract(e.event, '$.shown.started_from') FROM events AS e
            WHERE e.project = labels.project
              AND e.type = 'label_submitted'
              AND json_extract(e.event, '$.label_id') = labels.label)
        """
    )


def downgrade() -> None:
    op.drop_column("labels", "started_from")
