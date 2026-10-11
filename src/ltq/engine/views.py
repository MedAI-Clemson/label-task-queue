"""What the engine hands back to labelers: a claimed slot and the work packet for it."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from ltq.engine.events import Shown
from ltq.engine.tables import Row


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


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

    @classmethod
    def from_row(cls, row: Row) -> Slot:
        return cls.model_validate({name: row[name] for name in cls.model_fields})


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
