"""Read helpers over the derived tables, shared by the engine, explain and the exports."""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from labelflow.engine.config import Stage, WorkflowConfig
from labelflow.engine.errors import UnknownItem, UnknownStage, UnknownWorkflow
from labelflow.engine.tables import Row, Tables

_CONFIGS: dict[str, WorkflowConfig] = {}
"""Parsed configs by content hash. Safe to share: the same hash always means the same config."""


def did_you_mean(name: str, known: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(known), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def workflow_row(tables: Tables, workflow: str) -> Row:
    row = tables.get("workflows", workflow=workflow)
    if row is None:
        known = [r["workflow"] for r in tables.select("workflows")]
        raise UnknownWorkflow(
            f"There is no workflow named '{workflow}'.{did_you_mean(workflow, known)}"
        )
    return row


def config_at(tables: Tables, workflow: str, version: int) -> WorkflowConfig:
    """The workflow's config as of a given version."""
    row = tables.get("workflow_versions", workflow=workflow, version=version)
    if row is None:
        raise UnknownWorkflow(f"Workflow '{workflow}' has no config version {version}.")
    config = _CONFIGS.get(row["config_hash"])
    if config is None:
        config = _CONFIGS[row["config_hash"]] = WorkflowConfig.model_validate(row["config"])
    return config


def active_config(tables: Tables, workflow: str) -> tuple[int, WorkflowConfig]:
    """The version number and config currently in force for the workflow."""
    version = workflow_row(tables, workflow)["active_version"]
    return version, config_at(tables, workflow, version)


def stages_ever(tables: Tables, workflow: str) -> list[Stage]:
    """Every stage the workflow has had: the active version's stages in order, then the
    stages that edits removed, as last configured.

    Reports use it so that a removed stage's history stays visible.
    """
    active, config = active_config(tables, workflow)
    stages = list(config.stages)
    seen = set(config.stage_names)
    for version in range(active - 1, 0, -1):
        for stage in config_at(tables, workflow, version).stages:
            if stage.name not in seen:
                seen.add(stage.name)
                stages.append(stage)
    return stages


def stage_of(config: WorkflowConfig, stage: str) -> Stage:
    try:
        return config.stage(stage)
    except KeyError:
        raise UnknownStage(
            f"Workflow '{config.workflow}' has no stage named '{stage}'."
            + did_you_mean(stage, config.stage_names)
        ) from None


def workflow_item(tables: Tables, workflow: str, item: str) -> Row:
    """The item's row in the workflow; raises UnknownItem if it never entered it."""
    row = tables.get("workflow_items", workflow=workflow, item=item)
    if row is None:
        workflow_row(tables, workflow)
        raise UnknownItem(f"Item '{item}' is not in workflow '{workflow}'.")
    return row


def item_row(tables: Tables, workflow: str, item: str) -> Row:
    """The imported item (uri, content hash, properties) behind a workflow item."""
    dataset = workflow_row(tables, workflow)["dataset"]
    row = tables.get("items", dataset=dataset, item=item)
    if row is None:
        raise UnknownItem(f"Dataset '{dataset}' has no item '{item}'.")
    return row


def held_roles(tables: Tables, user: str) -> set[str]:
    """The roles the person holds now."""
    return {r["role"] for r in tables.select("role_assignments", user=user, active=True)}


def holders(tables: Tables, roles: Iterable[str]) -> list[str]:
    """Everyone who holds at least one of the roles now, sorted."""
    users = {
        r["user"]
        for role in roles
        for r in tables.select("role_assignments", role=role, active=True)
    }
    return sorted(users)


def role_holders(tables: Tables) -> dict[str, list[str]]:
    """Every role defined in the project, with the people holding it now."""
    return {r["role"]: holders(tables, [r["role"]]) for r in tables.select("roles")}


def labels_of(tables: Tables, workflow: str, item: str, stage: str | None = None) -> list[Row]:
    """The item's labels in the workflow (optionally one stage), in submission order."""
    where = {"workflow": workflow, "item": item} | ({"stage": stage} if stage else {})
    return sorted(tables.select("labels", **where), key=lambda r: r["label"])


def live_slots(tables: Tables, workflow: str, item: str, stage: str | None = None) -> list[Row]:
    """The item's open or claimed slots in the workflow (optionally one stage), by id."""
    where = {"workflow": workflow, "item": item} | ({"stage": stage} if stage else {})
    rows = tables.select("slots", status="open", **where)
    rows += tables.select("slots", status="claimed", **where)
    return sorted(rows, key=lambda r: r["slot"])


def next_id(tables: Tables, name: str) -> int:
    """The next unused slot or label id."""
    row = tables.get("counters", name=name)
    return 1 if row is None else row["value"] + 1


def export_status(tables: Tables, workflow: str, item: str) -> str:
    """in_progress, excluded, or the final-label status: final, unresolved or none."""
    status = workflow_item(tables, workflow, item)["status"]
    if status != "done":
        return status
    return tables.get("final_labels", workflow=workflow, item=item)["status"]


def and_list(names: Iterable[str], conjunction: str = "and") -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} {conjunction} {names[-1]}"
