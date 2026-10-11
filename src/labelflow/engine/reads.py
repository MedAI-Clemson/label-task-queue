"""The engine's read side: the views in ``views`` built from the derived tables.

Nothing here writes. ``Engine`` wraps each function in ``store.reading()``, so one call sees
one consistent state; the functions themselves only need the ``Tables`` interface (plus the
event log for config versions and the audit page).
"""

from __future__ import annotations

from collections import Counter

from labelflow.engine.config import Stage
from labelflow.engine.events import Event
from labelflow.engine.queries import (
    active_config,
    active_pause,
    config_at,
    dataset_row,
    item_row,
    labels_of,
    role_holders,
    role_row,
    stages_ever,
    workflow_row,
)
from labelflow.engine.store import Store
from labelflow.engine.tables import Row, Tables
from labelflow.engine.views import (
    ConfigVersion,
    ConsensusShown,
    Dataset,
    Item,
    ItemProgress,
    Page,
    Pause,
    PauseState,
    PriorLabel,
    Progress,
    Role,
    StageProgress,
    StageSummary,
    Workflow,
    WorkflowDetail,
    WorkPacket,
)


def _in_log_order(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda r: r["seq"])


# --- roles, datasets, items -----------------------------------------------------------------


def roles(tables: Tables) -> list[Role]:
    """Every role, in the order defined, with who holds it now."""
    holders = role_holders(tables)
    return [
        Role.from_row(row, holders=holders[row["role"]])
        for row in _in_log_order(tables.select("roles"))
    ]


def role(tables: Tables, name: str) -> Role:
    row = role_row(tables, name)
    return Role.from_row(row, holders=role_holders(tables)[name])


def datasets(tables: Tables) -> list[Dataset]:
    """Every dataset, in the order created, with its number of items."""
    return [dataset(tables, row["dataset"]) for row in _in_log_order(tables.select("datasets"))]


def dataset(tables: Tables, name: str) -> Dataset:
    row = dataset_row(tables, name)
    return Dataset.from_row(row, items=len(tables.select("items", dataset=name)))


def items(tables: Tables, dataset: str, offset: int, limit: int) -> Page[Item]:
    """One page of a dataset's items, in import order."""
    dataset_row(tables, dataset)
    rows = _in_log_order(tables.select("items", dataset=dataset))
    page = rows[offset : offset + limit]
    return Page(total=len(rows), offset=offset, limit=limit, rows=tuple(map(Item.from_row, page)))


# --- workflows ------------------------------------------------------------------------------


def workflows(tables: Tables) -> list[Workflow]:
    """Every workflow, in the order started."""
    return [_workflow(tables, row) for row in _in_log_order(tables.select("workflows"))]


def workflow(tables: Tables, name: str) -> WorkflowDetail:
    summary = _workflow(tables, workflow_row(tables, name))
    _, config = active_config(tables, name)
    return WorkflowDetail(**summary.model_dump(), config=config, progress=progress(tables, name))


def _workflow(tables: Tables, row: Row) -> Workflow:
    name = row["workflow"]
    _, config = active_config(tables, name)
    stages = tuple(
        StageSummary(
            name=stage.name,
            roles=tuple(stage.roles),
            paused=Pause.from_row(active_pause(tables, name, stage.name)),
        )
        for stage in config.stages
    )
    return Workflow.from_row(row, stages=stages, paused=Pause.from_row(active_pause(tables, name)))


def pauses(tables: Tables, workflow: str) -> PauseState:
    """What is paused in the workflow now: the workflow itself, and each stage."""
    _, config = active_config(tables, workflow)
    return PauseState(
        workflow=workflow,
        paused=Pause.from_row(active_pause(tables, workflow)),
        stages={
            name: Pause.from_row(active_pause(tables, workflow, name))
            for name in config.stage_names
        },
    )


def progress(tables: Tables, workflow: str) -> Progress:
    """Items by status and, per stage the workflow ever had, slots by status."""
    finals = {r["item"]: r["status"] for r in tables.select("final_labels", workflow=workflow)}
    entered = tables.select("workflow_items", workflow=workflow)
    by_status: Counter[str] = Counter()
    for row in entered:
        done = row["status"] == "done"
        by_status[finals[row["item"]] if done else row["status"]] += 1
    slots: dict[str, Counter[str]] = {s.name: Counter() for s in stages_ever(tables, workflow)}
    for row in tables.select("slots", workflow=workflow):
        slots.setdefault(row["stage"], Counter())[row["status"]] += 1
    return Progress(
        items=ItemProgress(entered=len(entered), **{k: by_status[k] for k in _ITEM_STATUSES}),
        stages=tuple(
            StageProgress(stage=stage, **{k: counts[k] for k in _SLOT_STATUSES})
            for stage, counts in slots.items()
        ),
    )


_ITEM_STATUSES = ("in_progress", "final", "unresolved", "none", "excluded")
_SLOT_STATUSES = ("open", "claimed", "filled", "cancelled")


def config_versions(store: Store, workflow: str) -> list[ConfigVersion]:
    """Every config version, oldest first; an edit's reason and diff come from its event."""
    active = workflow_row(store, workflow)["active_version"]
    edits = {e.config_version: e for e in store.events(workflow=workflow, type="config_activated")}
    versions = []
    for row in store.select("workflow_versions", workflow=workflow):
        edit = edits.get(row["version"])
        versions.append(
            ConfigVersion.from_row(
                row,
                active=row["version"] == active,
                config=config_at(store, workflow, row["version"]),
                reason=None if edit is None else edit.reason,
                diff=None if edit is None else edit.diff,
            )
        )
    return versions


def event_page(
    store: Store,
    offset: int,
    limit: int,
    workflow: str | None = None,
    item: str | None = None,
    type: str | None = None,
) -> Page[Event]:
    """One page of the event log, oldest first, filtered by workflow, item and/or type."""
    where = {"workflow": workflow, "item": item, "type": type}
    rows = store.events(offset=offset, limit=limit, **where)
    return Page(total=store.count_events(**where), offset=offset, limit=limit, rows=tuple(rows))


# --- for the labeler ------------------------------------------------------------------------


def work_packet(tables: Tables, slot: Row, stage: Stage) -> WorkPacket:
    """What the interface should show for a claimed slot of ``stage``.

    With ``show_prior`` (the default), that includes the item's labels from other stages of
    this workflow (ids and stages, not labeler names) and the most recent consensus check's
    detail. Item properties are never included.
    """
    workflow, item = slot["workflow"], slot["item"]
    imported = item_row(tables, workflow, item)
    priors: tuple[PriorLabel, ...] = ()
    consensus = None
    if stage.show_prior:
        priors = tuple(
            PriorLabel.from_row(r)
            for r in labels_of(tables, workflow, item)
            if r["stage"] != slot["stage"]
        )
        checks = tables.select("consensus", workflow=workflow, item=item)
        if checks:
            latest = max(checks, key=lambda r: r["seq"])
            consensus = ConsensusShown.from_row(latest, label_ids=tuple(latest["label_ids"]))
    return WorkPacket(
        workflow=workflow,
        slot=slot["slot"],
        stage=slot["stage"],
        item=item,
        uri=imported["uri"],
        content_hash=imported["content_hash"],
        show_prior=stage.show_prior,
        prior_labels=priors,
        consensus=consensus,
    )
