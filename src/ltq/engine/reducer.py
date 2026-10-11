"""The reducer: how each event changes the derived tables.

The reducer is pure bookkeeping. It never calls a label task, a clock or a random number
generator, and it makes no decisions: everything it writes (ids, draws, consensus answers,
timestamps) is already in the event. That is what makes replaying the log rebuild exactly
the same tables.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ltq.engine import events as ev
from ltq.engine.events import Event
from ltq.engine.tables import Tables


def apply_event(tables: Tables, event: Event) -> None:
    """Apply one logged event to the tables."""
    _REDUCERS[event.type](tables, event)


def _time(event: Event) -> str:
    return event.at.isoformat()


def _bump_counter(tables: Tables, name: str, value: int) -> None:
    """Remember the highest id handed out, so the next one continues the sequence."""
    row = tables.get("counters", name=name)
    if row is None:
        tables.insert("counters", {"name": name, "value": value})
    elif value > row["value"]:
        tables.update("counters", {"name": name}, {"value": value})


# --- project -----------------------------------------------------------------------------


def _role_defined(tables: Tables, e: ev.RoleDefined) -> None:
    tables.insert(
        "roles", {"role": e.role, "defined_by": e.actor, "defined_at": _time(e), "seq": e.seq}
    )


def _role_granted(tables: Tables, e: ev.RoleGranted) -> None:
    key = {"user": e.user, "role": e.role}
    changes = {"active": True, "since": _time(e), "seq": e.seq}
    if tables.get("role_assignments", **key) is None:
        tables.insert("role_assignments", key | changes)
    else:
        tables.update("role_assignments", key, changes)


def _role_revoked(tables: Tables, e: ev.RoleRevoked) -> None:
    tables.update(
        "role_assignments",
        {"user": e.user, "role": e.role},
        {"active": False, "since": _time(e), "seq": e.seq},
    )


def _dataset_created(tables: Tables, e: ev.DatasetCreated) -> None:
    tables.insert(
        "datasets",
        {"dataset": e.dataset, "created_by": e.actor, "created_at": _time(e), "seq": e.seq},
    )


def _item_imported(tables: Tables, e: ev.ItemImported) -> None:
    tables.insert(
        "items",
        {
            "dataset": e.dataset,
            "item": e.item,
            "uri": e.uri,
            "content_hash": e.content_hash,
            "properties": e.properties,
            "imported_by": e.actor,
            "imported_at": _time(e),
            "seq": e.seq,
        },
    )


# --- workflow ----------------------------------------------------------------------------


def _workflow_started(tables: Tables, e: ev.WorkflowStarted) -> None:
    tables.insert(
        "workflows",
        {
            "workflow": e.workflow,
            "dataset": e.dataset,
            "label_task": e.label_task,
            "seed": e.seed,
            "status": "running",
            "active_version": e.config_version,
            "started_by": e.actor,
            "started_at": _time(e),
            "seq": e.seq,
        },
    )
    tables.insert(
        "workflow_versions",
        {
            "workflow": e.workflow,
            "version": e.config_version,
            "config": e.config,
            "config_hash": e.config_hash,
            "activated_by": e.actor,
            "activated_at": _time(e),
            "seq": e.seq,
        },
    )


def _item_entered(tables: Tables, e: ev.ItemEntered) -> None:
    tables.insert(
        "workflow_items",
        {
            "workflow": e.workflow,
            "item": e.item,
            "status": "in_progress",
            "entered_at": _time(e),
            "entered_seq": e.seq,
            "closed_seq": None,
        },
    )


def _admission_decided(tables: Tables, e: ev.AdmissionDecided) -> None:
    tables.insert(
        "admissions",
        {
            "workflow": e.workflow,
            "stage": e.stage,
            "item": e.item,
            "source": e.source,
            "admitted": e.admitted,
            "draw": e.draw,
            "threshold": e.threshold,
            "labels_draw": e.labels_draw,
            "n_slots": len(e.slots),
            "config_version": e.config_version,
            "at": _time(e),
            "seq": e.seq,
            "completed": False,
            "outputs": [],
            "completed_seq": None,
        },
    )
    for new in e.slots:
        tables.insert(
            "slots",
            {
                "slot": new.slot,
                "workflow": e.workflow,
                "stage": e.stage,
                "item": e.item,
                "index": new.index,
                "reserved_for": new.reserved_for,
                "status": "open",
                "claimed_by": None,
                "claimed_at": None,
                "label": None,
                "config_version": e.config_version,
                "admitted_seq": e.seq,
                "closed_seq": None,
            },
        )
        _bump_counter(tables, "slot", new.slot)


def _slot_claimed(tables: Tables, e: ev.SlotClaimed) -> None:
    tables.update(
        "slots",
        {"slot": e.slot},
        {"status": "claimed", "claimed_by": e.user, "claimed_at": _time(e)},
    )


def _slot_released(tables: Tables, e: ev.SlotReleased) -> None:
    tables.update(
        "slots", {"slot": e.slot}, {"status": "open", "claimed_by": None, "claimed_at": None}
    )
    tables.insert(
        "releases",
        {
            "seq": e.seq,
            "workflow": e.workflow,
            "stage": e.stage,
            "item": e.item,
            "slot": e.slot,
            "user": e.user,
            "reason": e.reason,
            "at": _time(e),
        },
    )


def _label_submitted(tables: Tables, e: ev.LabelSubmitted) -> None:
    slot = tables.get("slots", slot=e.slot)
    tables.insert(
        "labels",
        {
            "label": e.label_id,
            "workflow": e.workflow,
            "stage": e.stage,
            "item": e.item,
            "slot": e.slot,
            "user": e.user,
            "roles": list(e.roles),
            "config_version": e.config_version,
            "value": e.label,
            "shown_content_hash": e.shown.content_hash,
            "shown_prior_ids": list(e.shown.prior_label_ids),
            "show_prior": e.shown.show_prior,
            "shown_consensus_rule": e.shown.consensus_rule_version,
            "label_task": e.label_task,
            "ontology_version": e.ontology_version,
            "duration_ms": e.duration_ms,
            "claimed_at": slot["claimed_at"] if slot else None,
            "submitted_at": _time(e),
            "seq": e.seq,
        },
    )
    tables.update(
        "slots",
        {"slot": e.slot},
        {"status": "filled", "label": e.label_id, "closed_seq": e.seq},
    )
    _bump_counter(tables, "label", e.label_id)


def _label_scored(tables: Tables, e: ev.LabelScored) -> None:
    tables.insert(
        "scores",
        {
            "label": e.label_id,
            "workflow": e.workflow,
            "stage": e.stage,
            "item": e.item,
            "user": e.user,
            "key_property": e.key_property,
            "key_status": e.key_status,
            "agreed": e.agreed,
            "rule_version": e.rule_version,
            "detail": e.detail,
            "seq": e.seq,
        },
    )


def _consensus_checked(tables: Tables, e: ev.ConsensusChecked) -> None:
    tables.insert(
        "consensus",
        {
            "workflow": e.workflow,
            "stage": e.stage,
            "item": e.item,
            "label_ids": list(e.label_ids),
            "agreed": e.agreed,
            "agreed_label": e.agreed_label,
            "rule_version": e.rule_version,
            "detail": e.detail,
            "config_version": e.config_version,
            "at": _time(e),
            "seq": e.seq,
        },
    )


def _stage_completed(tables: Tables, e: ev.StageCompleted) -> None:
    tables.update(
        "admissions",
        {"workflow": e.workflow, "stage": e.stage, "item": e.item},
        {"completed": True, "outputs": list(e.outputs), "completed_seq": e.seq},
    )


def _cancel(tables: Tables, slots: tuple[int, ...], seq: int) -> None:
    for slot in slots:
        tables.update(
            "slots",
            {"slot": slot},
            {"status": "cancelled", "claimed_by": None, "claimed_at": None, "closed_seq": seq},
        )


def _item_excluded(tables: Tables, e: ev.ItemExcluded) -> None:
    _cancel(tables, e.cancelled_slots, e.seq)
    tables.update(
        "workflow_items",
        {"workflow": e.workflow, "item": e.item},
        {"status": "excluded", "closed_seq": e.seq},
    )


def _slots_cancelled(tables: Tables, e: ev.SlotsCancelled) -> None:
    _cancel(tables, e.slots, e.seq)


def _item_finalized(tables: Tables, e: ev.ItemFinalized) -> None:
    tables.insert(
        "final_labels",
        {
            "workflow": e.workflow,
            "item": e.item,
            "status": e.status,
            "value": e.label,
            "source_stage": e.source_stage,
            "label_ids": list(e.label_ids),
            "rule": e.rule,
            "config_version": e.config_version,
            "at": _time(e),
            "seq": e.seq,
        },
    )
    tables.update(
        "workflow_items",
        {"workflow": e.workflow, "item": e.item},
        {"status": "done", "closed_seq": e.seq},
    )


_REDUCERS: dict[str, Callable[[Tables, Any], None]] = {
    "role_defined": _role_defined,
    "role_granted": _role_granted,
    "role_revoked": _role_revoked,
    "dataset_created": _dataset_created,
    "item_imported": _item_imported,
    "workflow_started": _workflow_started,
    "item_entered": _item_entered,
    "admission_decided": _admission_decided,
    "slot_claimed": _slot_claimed,
    "slot_released": _slot_released,
    "label_submitted": _label_submitted,
    "label_scored": _label_scored,
    "consensus_checked": _consensus_checked,
    "stage_completed": _stage_completed,
    "item_excluded": _item_excluded,
    "slots_cancelled": _slots_cancelled,
    "item_finalized": _item_finalized,
}
