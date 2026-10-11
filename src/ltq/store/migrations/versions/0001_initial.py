"""Create the event log and the derived tables.

A written-out copy of the schema as of this revision: do not generate it from
``ltq.engine.tables.SCHEMA`` at run time, and do not edit it once a database has been
upgraded to it; add a new revision instead.

Every table has a leading ``project`` column that is part of its primary key, so one database
can hold several projects. JSON values and ISO timestamps are TEXT. The database backs up the
engine's rules with: unique constraints (one label per person per item per workflow, one
label per slot), a unique partial index (one claimed slot per person per workflow), a trigger
on slots (a slot can only be claimed while open) and triggers that make the event log
append-only. Autogenerate does not see triggers, so they are written out here.

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admissions",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("admitted", sa.Boolean(), nullable=False),
        sa.Column("draw", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("labels_draw", sa.Float(), nullable=True),
        sa.Column("n_slots", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("outputs", sa.Text(), nullable=False),
        sa.Column("completed_seq", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("project", "workflow", "stage", "item", name="pk_admissions"),
    )
    op.create_table(
        "consensus",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("label_ids", sa.Text(), nullable=False),
        sa.Column("agreed", sa.Boolean(), nullable=False),
        sa.Column("agreed_label", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", "stage", "item", name="pk_consensus"),
    )
    op.create_table(
        "counters",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "name", name="pk_counters"),
    )
    op.create_table(
        "datasets",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "dataset", name="pk_datasets"),
    )
    op.create_table(
        "events",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("workflow", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("item", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("project", "seq", name="pk_events"),
        sa.UniqueConstraint("project", "hash", name="uq_events_project_hash"),
    )
    op.create_index(
        "ix_events_workflow_item", "events", ["project", "workflow", "item"], unique=False
    )
    op.create_table(
        "final_labels",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_stage", sa.Text(), nullable=True),
        sa.Column("label_ids", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", "item", name="pk_final_labels"),
    )
    op.create_table(
        "items",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("properties", sa.Text(), nullable=False),
        sa.Column("imported_by", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "dataset", "item", name="pk_items"),
    )
    op.create_table(
        "labels",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("label", sa.Integer(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("user", sa.Text(), nullable=False),
        sa.Column("roles", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("shown_content_hash", sa.Text(), nullable=False),
        sa.Column("shown_prior_ids", sa.Text(), nullable=False),
        sa.Column("show_prior", sa.Boolean(), nullable=False),
        sa.Column("shown_consensus_rule", sa.Text(), nullable=True),
        sa.Column("label_task", sa.Text(), nullable=False),
        sa.Column("ontology_version", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("claimed_at", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "label", name="pk_labels"),
        sa.UniqueConstraint("project", "slot", name="uq_labels_slot"),
        sa.UniqueConstraint(
            "project", "workflow", "item", "user", name="uq_labels_workflow_item_user"
        ),
    )
    op.create_table(
        "releases",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("user", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("project", "seq", name="pk_releases"),
    )
    op.create_table(
        "role_assignments",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("user", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("since", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "user", "role", name="pk_role_assignments"),
    )
    op.create_table(
        "roles",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("defined_by", sa.Text(), nullable=True),
        sa.Column("defined_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "role", name="pk_roles"),
    )
    op.create_table(
        "scores",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("label", sa.Integer(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("user", sa.Text(), nullable=False),
        sa.Column("key_property", sa.Text(), nullable=False),
        sa.Column("key_status", sa.Text(), nullable=False),
        sa.Column("agreed", sa.Boolean(), nullable=True),
        sa.Column("rule_version", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "label", name="pk_scores"),
    )
    op.create_table(
        "slots",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("reserved_for", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.Text(), nullable=True),
        sa.Column("label", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("admitted_seq", sa.Integer(), nullable=False),
        sa.Column("closed_seq", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("project", "slot", name="pk_slots"),
    )
    op.create_index(
        "ix_slots_workflow_status", "slots", ["project", "workflow", "status"], unique=False
    )
    op.create_index(
        "uq_slots_one_claim_per_person",
        "slots",
        ["project", "workflow", "claimed_by"],
        unique=True,
        sqlite_where=sa.text("status = 'claimed'"),
    )
    op.create_table(
        "workflow_items",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("entered_at", sa.Text(), nullable=False),
        sa.Column("entered_seq", sa.Integer(), nullable=False),
        sa.Column("closed_seq", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("project", "workflow", "item", name="pk_workflow_items"),
    )
    op.create_table(
        "workflow_versions",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", "version", name="pk_workflow_versions"),
    )
    op.create_table(
        "workflows",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("label_task", sa.Text(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.Column("started_by", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project", "workflow", name="pk_workflows"),
    )

    op.execute(
        """\
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: an event cannot be changed.');
END"""
    )
    op.execute(
        """\
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: an event cannot be deleted.');
END"""
    )
    op.execute(
        """\
CREATE TRIGGER events_in_order BEFORE INSERT ON events
WHEN NEW.seq IS NOT COALESCE((SELECT MAX(seq) FROM events WHERE project = NEW.project), 0) + 1
    OR NEW.prev_hash IS NOT COALESCE(
        (SELECT hash FROM events WHERE project = NEW.project AND seq = NEW.seq - 1),
        '0000000000000000000000000000000000000000000000000000000000000000')
BEGIN
    SELECT RAISE(ABORT, 'The event log is append-only: a new event must follow the last one.');
END"""
    )
    op.execute(
        """\
CREATE TRIGGER slots_claim_only_when_open BEFORE UPDATE OF status ON slots
WHEN NEW.status = 'claimed' AND OLD.status <> 'open'
BEGIN
    SELECT RAISE(ABORT, 'A slot can only be claimed while it is open.');
END"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER events_no_update")
    op.execute("DROP TRIGGER events_no_delete")
    op.execute("DROP TRIGGER events_in_order")
    op.execute("DROP TRIGGER slots_claim_only_when_open")
    op.drop_table("workflows")
    op.drop_table("workflow_versions")
    op.drop_table("workflow_items")
    op.drop_index(
        "uq_slots_one_claim_per_person",
        table_name="slots",
        sqlite_where=sa.text("status = 'claimed'"),
    )
    op.drop_index("ix_slots_workflow_status", table_name="slots")
    op.drop_table("slots")
    op.drop_table("scores")
    op.drop_table("roles")
    op.drop_table("role_assignments")
    op.drop_table("releases")
    op.drop_table("labels")
    op.drop_table("items")
    op.drop_table("final_labels")
    op.drop_index("ix_events_workflow_item", table_name="events")
    op.drop_table("events")
    op.drop_table("datasets")
    op.drop_table("counters")
    op.drop_table("consensus")
    op.drop_table("admissions")
