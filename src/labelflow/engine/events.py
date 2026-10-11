"""The event log's records.

Every change in a project is one event appended to an append-only log. Events are frozen
Pydantic models, one class per ``type``, and serialize to JSON. Each event carries
everything the reducer needs to apply it (draw values, consensus answers, new ids,
timestamps), so replaying the log always rebuilds the same state.

Every event records ``seq`` (its position in the log), ``at`` (UTC), ``actor`` (a user id,
or None when the workflow decided by itself), the ``workflow``, ``stage`` and ``item`` it is
about where relevant, the workflow ``config_version`` in force, and ``because``: the rule
that caused it, in plain language, naming the config line.

Fingerprint chain: each event stores the hash of the event before it (``prev_hash``) and its
own ``hash = sha256(prev_hash + canonical JSON of the event without hash)``. Changing or
removing any event breaks the chain from that point on; ``verify_chain`` finds where.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from labelflow.engine.errors import BrokenChain

GENESIS_HASH = "0" * 64
"""The ``prev_hash`` of the first event in a log."""

JsonDict = dict[str, JsonValue]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Shown(_Frozen):
    """References to what the interface showed the labeler, reported with a label."""

    content_hash: str = Field(description="The content hash of the image shown.")
    prior_label_ids: tuple[int, ...] = Field((), description="Earlier labels shown.")
    show_prior: bool = Field(description="Whether earlier labels were offered.")
    consensus_rule_version: str | None = Field(
        None, description="The consensus rule version behind any agree/disagree flags shown."
    )


class NewSlot(_Frozen):
    """A slot opened by an admission."""

    slot: int
    index: int
    """1-based position among the item's slots in the stage."""
    reserved_for: str | None = None
    """Set when ``labels_per_item: all`` reserved the slot for one person."""


class _Event(_Frozen):
    type: str
    seq: int | None = None
    at: AwareDatetime
    actor: str | None = None
    workflow: str | None = None
    stage: str | None = None
    item: str | None = None
    config_version: int | None = None
    because: str
    prev_hash: str | None = None
    hash: str | None = None


# --- project: roles, datasets, items ---------------------------------------------------


class RoleDefined(_Event):
    type: Literal["role_defined"] = "role_defined"
    role: str


class RoleGranted(_Event):
    type: Literal["role_granted"] = "role_granted"
    user: str
    role: str
    reason: str


class RoleRevoked(_Event):
    type: Literal["role_revoked"] = "role_revoked"
    user: str
    role: str
    reason: str


class DatasetCreated(_Event):
    type: Literal["dataset_created"] = "dataset_created"
    dataset: str


class ItemImported(_Event):
    type: Literal["item_imported"] = "item_imported"
    dataset: str
    uri: str
    content_hash: str
    properties: JsonDict


# --- workflow ----------------------------------------------------------------------------


class WorkflowStarted(_Event):
    type: Literal["workflow_started"] = "workflow_started"
    dataset: str
    label_task: str
    seed: int
    config: JsonDict
    """The full config of version 1, every default filled in."""
    config_hash: str


class ConfigActivated(_Event):
    """An admin activated a new config version of a running workflow (a forward-only edit).

    ``config_version`` is the new version. It governs only decisions made from this event
    on; nothing else changes at activation. The event carries what the admin saw in the
    change preview: the classified changes, the diff and the counts (see ``edits``).
    """

    type: Literal["config_activated"] = "config_activated"
    base_version: int
    """The version this one replaces (the one the change was previewed against)."""
    config: JsonDict
    """The full config of the new version, every default filled in."""
    config_hash: str
    changes: tuple[JsonDict, ...]
    """Each change, as ``edits.Change``: kind, stage, before, after, summary, effect."""
    diff: str
    """A unified diff of the two resolved YAML documents."""
    counts: JsonDict
    """The preview's counts of what the change touches (see ``ChangePreview.counts``)."""
    warnings: tuple[str, ...] = ()
    reason: str = ""


class ItemEntered(_Event):
    """An item of the workflow's dataset entered the workflow."""

    type: Literal["item_entered"] = "item_entered"
    dataset: str


class AdmissionDecided(_Event):
    """An item arrived at a stage; it was admitted or not. Admission opens slots."""

    type: Literal["admission_decided"] = "admission_decided"
    source: str
    """Where the item came from: ``dataset`` or the stage input, e.g. ``annotate.disagreed``."""
    admitted: bool
    draw: float | None
    """The hashed draw; None when ``sample`` is 1 and no draw was needed."""
    threshold: float
    """The stage's ``sample``; the item is admitted when ``draw < threshold``."""
    labels_per_item: JsonValue
    """The stage's ``labels_per_item`` setting, as written in the config."""
    labels_draw: float | None = None
    """The separate draw for the ``{count, fraction}`` form, else None."""
    slots: tuple[NewSlot, ...] = ()


class SlotClaimed(_Event):
    type: Literal["slot_claimed"] = "slot_claimed"
    slot: int
    user: str
    roles: tuple[str, ...]
    """Every role the person held at the moment of the claim."""


class SlotReleased(_Event):
    type: Literal["slot_released"] = "slot_released"
    slot: int
    user: str
    reason: str


class LabelSubmitted(_Event):
    type: Literal["label_submitted"] = "label_submitted"
    slot: int
    label_id: int
    user: str
    roles: tuple[str, ...]
    """Every role the person held at the moment of submitting."""
    label: JsonDict
    """The label exactly as submitted."""
    shown: Shown
    label_task: str
    ontology_version: str
    duration_ms: int | None = None
    """Time from claim to submit, as reported by the interface."""


class LabelScored(_Event):
    """A label compared with a key stored on the item (``score_against``)."""

    type: Literal["label_scored"] = "label_scored"
    label_id: int
    user: str
    key_property: str
    key_status: Literal["scored", "no_key", "invalid_key"]
    agreed: bool | None = None
    rule_version: str | None = None
    detail: JsonDict = {}


class ConsensusChecked(_Event):
    """The label task's answer to "do these labels agree?" for one item in one stage."""

    type: Literal["consensus_checked"] = "consensus_checked"
    label_ids: tuple[int, ...]
    agreed: bool
    agreed_label: JsonDict | None = None
    rule_version: str
    detail: JsonDict = {}


class StageCompleted(_Event):
    """No slot of the item is open or claimed in the stage any more; it leaves the stage."""

    type: Literal["stage_completed"] = "stage_completed"
    label_ids: tuple[int, ...]
    outputs: tuple[str, ...]
    """The outputs the item goes to, e.g. ``("annotate", "annotate.disagreed")``."""


class ItemExcluded(_Event):
    """An admin took the item out of the workflow.

    An item that already had its final label may be excluded too: its final-label record and
    its labels are kept as history, and only its status changes.
    """

    type: Literal["item_excluded"] = "item_excluded"
    reason: str
    cancelled_slots: tuple[int, ...]


class SlotsCancelled(_Event):
    """An admin cancelled the item's open (unclaimed) slots in one stage."""

    type: Literal["slots_cancelled"] = "slots_cancelled"
    reason: str
    slots: tuple[int, ...]


class WorkflowPaused(_Event):
    """An admin paused the whole workflow: no slot of it can be claimed until it is resumed.

    A pause stops claims only. Items keep arriving and being admitted, held claims can still
    be submitted or released, and consensus and finalization go on.
    """

    type: Literal["workflow_paused"] = "workflow_paused"
    reason: str


class WorkflowResumed(_Event):
    """An admin resumed a paused workflow. Stages paused on their own stay paused."""

    type: Literal["workflow_resumed"] = "workflow_resumed"
    reason: str


class StagePaused(_Event):
    """An admin paused one stage: none of its slots can be claimed until it is resumed."""

    type: Literal["stage_paused"] = "stage_paused"
    reason: str


class StageResumed(_Event):
    type: Literal["stage_resumed"] = "stage_resumed"
    reason: str


class ItemFinalized(_Event):
    """The item is done: its one final label for this workflow (or why there is none)."""

    type: Literal["item_finalized"] = "item_finalized"
    status: Literal["final", "unresolved", "none"]
    label: JsonDict | None = None
    source_stage: str | None = None
    label_ids: tuple[int, ...] = ()
    rule: str


Event = Annotated[
    RoleDefined
    | RoleGranted
    | RoleRevoked
    | DatasetCreated
    | ItemImported
    | WorkflowStarted
    | ConfigActivated
    | ItemEntered
    | AdmissionDecided
    | SlotClaimed
    | SlotReleased
    | LabelSubmitted
    | LabelScored
    | ConsensusChecked
    | StageCompleted
    | ItemExcluded
    | SlotsCancelled
    | WorkflowPaused
    | WorkflowResumed
    | StagePaused
    | StageResumed
    | ItemFinalized,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def canonical_json(data: Any) -> str:
    """JSON with sorted keys and no whitespace: the same data always gives the same text."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def event_to_json(event: Event) -> str:
    return canonical_json(event.model_dump(mode="json"))


def event_from_json(text: str | bytes) -> Event:
    return EVENT_ADAPTER.validate_json(text)


def event_hash(event: Event) -> str:
    """``sha256(prev_hash + canonical JSON of the event without its hash)``."""
    body = canonical_json(event.model_dump(mode="json", exclude={"hash"}))
    return hashlib.sha256((str(event.prev_hash) + body).encode("utf-8")).hexdigest()


def seal(event: Event, seq: int, prev_hash: str) -> Event:
    """Give a new event its place in the log: its ``seq``, ``prev_hash`` and ``hash``."""
    if event.seq is not None or event.hash is not None:
        raise ValueError("Only a new event can be sealed; this one already has a seq or hash.")
    placed = event.model_copy(update={"seq": seq, "prev_hash": prev_hash})
    return placed.model_copy(update={"hash": event_hash(placed)})


def verify_chain(events: Iterable[Event]) -> int:
    """Check the fingerprint chain of a whole log; return the number of events checked.

    Raises BrokenChain naming the first event that does not verify: one whose content was
    changed, whose predecessor was changed or removed, or that is out of sequence.
    """
    prev_hash = GENESIS_HASH
    count = 0
    for count, event in enumerate(events, start=1):
        if event.seq != count:
            raise BrokenChain(f"Event {event.seq} is out of place: expected event {count}.")
        if event.prev_hash != prev_hash:
            raise BrokenChain(
                f"Event {event.seq} does not follow event {count - 1}: the earlier event was "
                "changed or removed."
            )
        if event.hash != event_hash(event):
            raise BrokenChain(f"Event {event.seq} was changed after it was logged.")
        prev_hash = event.hash
    return count
