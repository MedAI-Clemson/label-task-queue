"""What the engine hands back: frozen, plain objects built from the derived tables.

The first group is what labelers work with (a claimed slot, its work packet). The second
is what the web layer lists and shows: roles, datasets, items, workflows, config versions,
progress, pauses, and pages of any of those. None of it is ever written back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from labelflow.engine.config import WorkflowConfig
from labelflow.engine.events import Shown
from labelflow.engine.tables import Row


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)

    @classmethod
    def from_row(cls, row: Row, **extra: Any):
        """Build the view from a table row with the same column names, plus ``extra``."""
        fields = {name: row[name] for name in cls.model_fields if name in row}
        return cls.model_validate(fields | extra)


# --- for labelers ---------------------------------------------------------------------------


class Slot(_View):
    """One needed label for one item in one stage."""

    slot: int
    workflow: str
    stage: str
    item: str
    index: int
    status: Literal["open", "claimed", "filled", "cancelled"]
    reserved_for: str | None
    claimed_by: str | None
    claimed_at: datetime | None
    label: int | None
    config_version: int


class PriorLabel(_View):
    """An earlier label of the item, from another stage. Labeler names are not shown."""

    label: int
    stage: str
    value: dict[str, JsonValue]


class ConsensusShown(_View):
    """The most recent consensus check on the item, for the interface's agree/disagree flags."""

    stage: str
    label_ids: tuple[int, ...]
    agreed: bool
    rule_version: str
    detail: dict[str, Any]


class WorkPacket(_View):
    """Everything the interface should show for a claimed slot, and nothing more.

    Item properties (such as a gold-standard key) are never included.
    """

    workflow: str
    slot: int
    stage: str
    item: str
    uri: str
    content_hash: str
    show_prior: bool
    prior_labels: tuple[PriorLabel, ...] = ()
    consensus: ConsensusShown | None = None

    def shown(self) -> Shown:
        """The references to report with the label, if the interface showed all of this."""
        return Shown(
            content_hash=self.content_hash,
            prior_label_ids=tuple(p.label for p in self.prior_labels),
            show_prior=self.show_prior,
            consensus_rule_version=self.consensus.rule_version if self.consensus else None,
        )


# --- for the project's pages ----------------------------------------------------------------


class Page[T](_View):
    """One page of a longer list, in the list's own order."""

    total: int
    offset: int
    limit: int
    rows: tuple[T, ...]


class NewItem(_View):
    """An item to import: the caller hashes the image, the engine never reads it."""

    item: str
    uri: str
    content_hash: str
    properties: dict[str, JsonValue] = {}


class Role(_View):
    role: str
    defined_by: str | None
    defined_at: datetime
    holders: tuple[str, ...]
    """The people who hold the role now, sorted."""


class Dataset(_View):
    dataset: str
    created_by: str | None
    created_at: datetime
    items: int


class Item(_View):
    dataset: str
    item: str
    uri: str
    content_hash: str
    properties: dict[str, JsonValue]
    imported_by: str | None
    imported_at: datetime


class Pause(_View):
    """A pause in force: who paused, since when, and why."""

    by: str | None
    since: datetime
    reason: str

    @classmethod
    def from_row(cls, row: Row | None) -> Pause | None:
        if row is None:
            return None
        return cls(by=row["by"], since=row["at"], reason=row["reason"])


class PauseState(_View):
    """What is paused in a workflow now. A pause stops claims only.

    ``stages`` lists the stages of the active config in order, each with its own pause or
    None. The workflow's pause and the stages' pauses are independent: a slot can be claimed
    only when neither its workflow nor its stage is paused.
    """

    workflow: str
    paused: Pause | None
    stages: dict[str, Pause | None]


class StageSummary(_View):
    name: str
    roles: tuple[str, ...]
    """The stage's ``assign_to``."""
    paused: Pause | None


class Workflow(_View):
    """A workflow as it runs now: its active config's stages and what is paused."""

    workflow: str
    dataset: str
    label_task: str
    seed: int
    status: str
    active_version: int
    started_by: str | None
    started_at: datetime
    stages: tuple[StageSummary, ...]
    paused: Pause | None


class ItemProgress(_View):
    """The workflow's items by status; ``entered`` is the sum of the others."""

    entered: int
    in_progress: int
    final: int
    unresolved: int
    none: int
    excluded: int


class StageProgress(_View):
    """The slots of one stage by status (every stage the workflow ever had)."""

    stage: str
    open: int
    claimed: int
    filled: int
    cancelled: int


class Progress(_View):
    items: ItemProgress
    stages: tuple[StageProgress, ...]


class WorkflowDetail(Workflow):
    config: WorkflowConfig
    """The config in force (see ``resolved_yaml`` and ``to_mermaid`` for views of it)."""
    progress: Progress


class ConfigVersion(_View):
    version: int
    active: bool
    activated_by: str | None
    activated_at: datetime
    config_hash: str
    config: WorkflowConfig
    reason: str | None
    """Why the edit was made; None for version 1."""
    diff: str | None
    """What the edit changed, as a unified diff; None for version 1."""
