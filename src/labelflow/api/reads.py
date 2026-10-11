"""Read-only views of a project that the engine has no command for.

The engine answers questions about one item or one slot (explain, work packets, exports).
The API also lists roles, datasets, items, workflows and config versions, counts progress,
and shows the event log; those reads live here. They use the engine's store and its read
helpers, and change nothing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import sqlalchemy as sa

from labelflow.api import db
from labelflow.engine import UnknownDataset, UnknownRole
from labelflow.engine.config import did_you_mean, resolved_yaml, to_mermaid
from labelflow.engine.queries import (
    active_config,
    active_pause,
    config_at,
    held_roles,
    role_holders,
    workflow_row,
)
from labelflow.engine.store import Store
from labelflow.engine.tables import Row

# --- roles ----------------------------------------------------------------------------------


def roles(store: Store) -> list[dict[str, Any]]:
    """Every role of the project with the people who hold it now."""
    with store.transaction():
        holders = role_holders(store)
        return [
            {
                "role": row["role"],
                "defined_by": row["defined_by"],
                "defined_at": row["defined_at"],
                "holders": holders[row["role"]],
            }
            for row in sorted(store.select("roles"), key=lambda r: r["seq"])
        ]


def role(store: Store, name: str) -> dict[str, Any]:
    with store.transaction():
        for row in roles(store):
            if row["role"] == name:
                return row
        known = [r["role"] for r in store.select("roles")]
    raise UnknownRole(f"There is no role named '{name}'.{did_you_mean(name, known)}")


# --- datasets and items ---------------------------------------------------------------------


def datasets(store: Store) -> list[dict[str, Any]]:
    """Every dataset with its number of items."""
    with store.transaction():
        return [
            {
                "dataset": row["dataset"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "items": len(store.select("items", dataset=row["dataset"])),
            }
            for row in sorted(store.select("datasets"), key=lambda r: r["seq"])
        ]


def require_dataset(store: Store, name: str) -> None:
    if store.get("datasets", dataset=name) is None:
        known = [r["dataset"] for r in store.select("datasets")]
        raise UnknownDataset(f"There is no dataset named '{name}'.{did_you_mean(name, known)}")


def dataset(store: Store, name: str) -> dict[str, Any]:
    with store.transaction():
        require_dataset(store, name)
        return next(row for row in datasets(store) if row["dataset"] == name)


def item_ids(store: Store, dataset_name: str) -> set[str]:
    """The ids of the items a dataset already holds."""
    with store.transaction():
        require_dataset(store, dataset_name)
        return {row["item"] for row in store.select("items", dataset=dataset_name)}


def items_page(store: Store, dataset_name: str, limit: int, offset: int) -> tuple[int, list[Row]]:
    """The dataset's items in import order: the total, and one page of them."""
    with store.transaction():
        require_dataset(store, dataset_name)
        rows = sorted(store.select("items", dataset=dataset_name), key=lambda r: r["seq"])
    return len(rows), rows[offset : offset + limit]


# --- workflows ------------------------------------------------------------------------------


def workflow_summaries(store: Store) -> list[dict[str, Any]]:
    """Every workflow of the project, in the order they were started."""
    with store.transaction():
        return [
            _summary(store, row)
            for row in sorted(store.select("workflows"), key=lambda r: r["seq"])
        ]


def _summary(store: Store, row: Row) -> dict[str, Any]:
    _, config = active_config(store, row["workflow"])
    return {
        "workflow": row["workflow"],
        "dataset": row["dataset"],
        "label_task": row["label_task"],
        "status": row["status"],
        "active_version": row["active_version"],
        "started_by": row["started_by"],
        "started_at": row["started_at"],
        "stages": [{"name": s.name, "assign_to": list(s.roles)} for s in config.stages],
    }


def workflow_roles(store: Store, workflow: str) -> set[str]:
    """The roles the workflow's active config assigns stages to."""
    with store.transaction():
        _, config = active_config(store, workflow)
        return {role for stage in config.stages for role in stage.roles}


def user_roles(store: Store, user: str) -> set[str]:
    with store.transaction():
        return held_roles(store, user)


def _pause(row: Row | None) -> dict[str, Any]:
    """``paused`` and, when paused, who paused it, since when and why."""
    if row is None:
        return {"paused": False, "pause": None}
    return {"paused": True, "pause": {"by": row["by"], "since": row["at"], "reason": row["reason"]}}


def pauses(store: Store, workflow: str) -> dict[str, Any]:
    """What is paused in the workflow: the workflow itself, and each stage of the active
    config. The two are independent; a stage's slots can be claimed only when neither is."""
    with store.transaction():
        _, config = active_config(store, workflow)
        return {
            "workflow": workflow,
            **_pause(active_pause(store, workflow)),
            "stages": [
                {"stage": name, **_pause(active_pause(store, workflow, name))}
                for name in config.stage_names
            ],
        }


def workflow_detail(store: Store, workflow: str) -> dict[str, Any]:
    """The workflow as it runs now: active version, resolved config, diagram, progress and
    what is paused."""
    with store.transaction():
        row = workflow_row(store, workflow)
        config = active_config(store, workflow)[1]
        summary = _summary(store, row)
        paused = pauses(store, workflow)
        stage_pauses = {entry["stage"]: entry for entry in paused["stages"]}
        return {
            **summary,
            "paused": paused["paused"],
            "pause": paused["pause"],
            "stages": [
                stage | {k: stage_pauses[stage["name"]][k] for k in ("paused", "pause")}
                for stage in summary["stages"]
            ],
            "seed": row["seed"],
            "resolved_yaml": resolved_yaml(config),
            "diagram": to_mermaid(config),
            "progress": progress(store, workflow),
        }


def progress(store: Store, workflow: str) -> dict[str, Any]:
    """Items by status, and slots by stage."""
    with store.transaction():
        _, config = active_config(store, workflow)
        finals = {r["item"]: r["status"] for r in store.select("final_labels", workflow=workflow)}
        statuses: Counter[str] = Counter()
        entered = store.select("workflow_items", workflow=workflow)
        for row in entered:
            done = row["status"] == "done"
            statuses[finals.get(row["item"], "none") if done else row["status"]] += 1
        slots: dict[str, Counter[str]] = {name: Counter() for name in config.stage_names}
        for row in store.select("slots", workflow=workflow):
            slots.setdefault(row["stage"], Counter())[row["status"]] += 1
    return {
        "items": {
            "entered": len(entered),
            "in_progress": statuses["in_progress"],
            "final": statuses["final"],
            "unresolved": statuses["unresolved"],
            "none": statuses["none"],
            "excluded": statuses["excluded"],
        },
        "stages": [
            {
                "stage": stage,
                "open": counts["open"],
                "claimed": counts["claimed"],
                "filled": counts["filled"],
                "cancelled": counts["cancelled"],
            }
            for stage, counts in slots.items()
        ],
    }


def versions(store: Store, database: sa.Engine, project: str, workflow: str) -> list[dict]:
    """Every config version of a workflow, oldest first, with who activated it and why."""
    with store.transaction():
        active = workflow_row(store, workflow)["active_version"]
        rows = sorted(
            store.select("workflow_versions", workflow=workflow), key=lambda r: r["version"]
        )
        _, activations = audit(
            database, project, 10_000, 0, workflow, event_type="config_activated"
        )
        edits = {event["config_version"]: event for event in activations}
        result = []
        for row in rows:
            edit = edits.get(row["version"], {})
            result.append(
                {
                    "version": row["version"],
                    "active": row["version"] == active,
                    "activated_by": row["activated_by"],
                    "activated_at": row["activated_at"],
                    "config_hash": row["config_hash"],
                    "reason": edit.get("reason"),
                    "diff": edit.get("diff"),
                    "resolved_yaml": resolved_yaml(config_at(store, workflow, row["version"])),
                }
            )
        return result


# --- the event log --------------------------------------------------------------------------


def audit(
    database: sa.Engine,
    project: str,
    limit: int,
    offset: int,
    workflow: str | None = None,
    item: str | None = None,
    event_type: str | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """A page of the project's events in log order, and how many events match in all."""
    events = db.table("events")
    where = [events.c.project == project]
    for column, value in (
        (events.c.workflow, workflow),
        (events.c.item, item),
        (events.c.type, event_type),
    ):
        if value is not None:
            where.append(column == value)
    count = sa.select(sa.func.count()).select_from(events).where(*where)
    page = (
        sa.select(events.c.event).where(*where).order_by(events.c.seq).limit(limit).offset(offset)
    )
    with db.reading(database) as connection:
        total = connection.execute(count).scalar_one()
        return total, list(connection.execute(page).scalars())
