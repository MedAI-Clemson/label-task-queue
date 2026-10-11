"""``explain(item)``: why an item went where it went, and why it is waiting, if it is.

The trace is every logged event about the item in the workflow, in order, each with its
``because``. When the item is still in progress, each of its open or claimed slots gets a
plain-language reason, built from the same eligibility rules ``request_work`` uses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ltq.engine import events as ev
from ltq.engine.eligibility import Person, load_person, why_not
from ltq.engine.events import Event, canonical_json
from ltq.engine.queries import (
    active_config,
    and_list,
    export_status,
    holders,
    item_row,
    live_slots,
    workflow_item,
    workflow_row,
)
from ltq.engine.store import Store
from ltq.engine.tables import Row, Tables


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class TraceEntry(_View):
    seq: int
    at: datetime
    type: str
    stage: str | None
    actor: str | None
    summary: str
    because: str


class WaitingSlot(_View):
    slot: int
    stage: str
    index: int
    status: str
    reason: str


class FinalLabel(_View):
    status: str
    label: Any
    source_stage: str | None
    label_ids: tuple[int, ...]
    rule: str
    config_version: int


class Explanation(_View):
    workflow: str
    item: str
    dataset: str
    uri: str
    content_hash: str
    status: str
    """in_progress, final, unresolved, none or excluded."""
    final: FinalLabel | None
    trace: tuple[TraceEntry, ...]
    waiting: tuple[WaitingSlot, ...]

    def render(self) -> str:
        """The explanation as plain text."""
        lines = [
            f"item {self.item}  in workflow {self.workflow}  status: {self.status}",
            f"  uri {self.uri}  (content hash {_short(self.content_hash, 16)})",
        ]
        if self.final is not None:
            what = {
                "final": f"final label {_short(canonical_json(self.final.label))}",
                "unresolved": "unresolved: the labels disagreed",
                "none": "no final label",
            }[self.final.status]
            lines.append(f"  {what}  (config v{self.final.config_version})")
            lines.append(f"  rule: {self.final.rule}")
        lines.append("")
        for entry in self.trace:
            stamp = entry.at.strftime("%m-%d %H:%M")
            lines.append(f"{stamp}  {_VERBS.get(entry.type, entry.type):<12} {entry.summary}")
            lines.append(f"{'':11}  {'because':<12} {entry.because}")
        if self.waiting:
            lines += ["", "waiting:"]
            lines += [f"  - {w.reason}" for w in self.waiting]
        return "\n".join(lines) + "\n"


_VERBS = {
    "item_imported": "imported",
    "item_entered": "entered",
    "admission_decided": "admission",
    "slot_claimed": "claimed",
    "slot_released": "released",
    "label_submitted": "labeled",
    "label_scored": "scored",
    "consensus_checked": "consensus",
    "stage_completed": "left stage",
    "item_excluded": "excluded",
    "slots_cancelled": "cancelled",
    "item_finalized": "final",
}


def explain(store: Store, workflow: str, item: str) -> Explanation:
    """Build the explanation of one item in one workflow."""
    tables = store
    row = workflow_item(tables, workflow, item)
    dataset = workflow_row(tables, workflow)["dataset"]
    imported = item_row(tables, workflow, item)
    about = [
        e
        for e in store.events(item=item)
        if e.workflow == workflow or (e.type == "item_imported" and e.dataset == dataset)
    ]
    final_row = tables.get("final_labels", workflow=workflow, item=item)
    final = None
    if final_row is not None:
        final = FinalLabel(
            status=final_row["status"],
            label=final_row["value"],
            source_stage=final_row["source_stage"],
            label_ids=tuple(final_row["label_ids"]),
            rule=final_row["rule"],
            config_version=final_row["config_version"],
        )
    waiting: tuple[WaitingSlot, ...] = ()
    if row["status"] == "in_progress":
        waiting = tuple(_waiting(tables, workflow, item))
    return Explanation(
        workflow=workflow,
        item=item,
        dataset=dataset,
        uri=imported["uri"],
        content_hash=imported["content_hash"],
        status=export_status(tables, workflow, item),
        final=final,
        trace=tuple(
            TraceEntry(
                seq=e.seq,
                at=e.at,
                type=e.type,
                stage=e.stage,
                actor=e.actor,
                summary=_summary(e),
                because=e.because,
            )
            for e in about
        ),
        waiting=waiting,
    )


# --- why a slot is waiting -----------------------------------------------------------------


def _waiting(tables: Tables, workflow: str, item: str) -> list[WaitingSlot]:
    _, config = active_config(tables, workflow)
    result = []
    for slot in live_slots(tables, workflow, item):
        name = f"slot {slot['index']} of {slot['stage']} (id {slot['slot']})"
        if slot["status"] == "claimed":
            since = datetime.fromisoformat(slot["claimed_at"]).strftime("%Y-%m-%d %H:%M UTC")
            reason = f"{name} is claimed by {slot['claimed_by']} since {since}"
        else:
            roles = config.stage(slot["stage"]).roles if slot["stage"] in config.stage_names else ()
            reason = f"{name} is open; " + _why_open(tables, workflow, slot, roles)
        result.append(
            WaitingSlot(
                slot=slot["slot"],
                stage=slot["stage"],
                index=slot["index"],
                status=slot["status"],
                reason=reason,
            )
        )
    return result


def _why_open(tables: Tables, workflow: str, slot: Row, roles: tuple[str, ...]) -> str:
    """Who could claim an open slot, and why nobody has."""
    role_text = and_list(roles, "or")
    reserved = slot["reserved_for"]
    if reserved:
        person = load_person(tables, workflow, reserved)
        reason = why_not(person, slot, roles)
        if reason is None:
            return f"it is reserved for {reserved}; {_ready(person)}"
        return f"it is reserved for {reserved}, so nobody can claim it: {reason}"
    people = holders(tables, roles)
    if not people:
        return f"nobody holds role {role_text}, so nobody can claim it"
    blocked, ready = [], []
    for user in people:
        person = load_person(tables, workflow, user)
        reason = why_not(person, slot, roles)
        if reason is None:
            ready.append(_ready(person))
        else:
            blocked.append(reason)
    held = f"role {role_text} is held by {and_list(people)}"
    if ready:
        return "; ".join([held, *blocked, *ready])
    if len(people) == 1:
        return (
            f"nobody can claim it: {people[0]} is the only holder of role {role_text}, and "
            f"{blocked[0]}"
        )
    return f"{held}; nobody can claim it: " + "; ".join(blocked)


def _ready(person: Person) -> str:
    """What an eligible person is doing instead of claiming the slot."""
    if person.claims:
        claim = next(iter(person.claims.values()))
        return (
            f"{person.user} can claim it but is working on slot {claim['slot']} "
            f"(item {claim['item']}, {claim['stage']}) first"
        )
    return f"{person.user} can claim it and has not asked for work yet"


# --- one line per event --------------------------------------------------------------------


def _summary(e: Event) -> str:
    match e:
        case ev.ItemImported():
            return f"into dataset {e.dataset} ({e.uri})"
        case ev.ItemEntered():
            return f"workflow {e.workflow}"
        case ev.AdmissionDecided(admitted=False):
            return f"not admitted to {e.stage}"
        case ev.AdmissionDecided():
            ids = and_list(str(s.slot) for s in e.slots)
            opened = f"{len(e.slots)} slot(s) opened ({ids})" if e.slots else "no slots opened"
            return f"admitted to {e.stage}: {opened}"
        case ev.SlotClaimed():
            return f"slot {e.slot} of {e.stage} by {e.user} ({', '.join(e.roles)})"
        case ev.SlotReleased():
            return f"slot {e.slot} of {e.stage} by {e.user}"
        case ev.LabelSubmitted():
            took = f" ({_duration(e.duration_ms)})" if e.duration_ms is not None else ""
            return (
                f"label {e.label_id} by {e.user} ({', '.join(e.roles)}) in {e.stage}: "
                f"{_short(canonical_json(e.label))}{took}"
            )
        case ev.LabelScored():
            verdict = {
                "no_key": "no key",
                "invalid_key": "invalid key",
                "scored": "agreed" if e.agreed else "did not agree",
            }[e.key_status]
            return f"label {e.label_id} against {e.key_property}: {verdict}"
        case ev.ConsensusChecked():
            ids = and_list(str(i) for i in e.label_ids)
            return f"labels {ids} in {e.stage}: {'agreed' if e.agreed else 'disagreed'}"
        case ev.StageCompleted():
            if not e.outputs:
                return f"{e.stage} with no labels"
            return f"{e.stage} with {len(e.label_ids)} label(s) -> {', '.join(e.outputs)}"
        case ev.ItemExcluded():
            ids = and_list(str(i) for i in e.cancelled_slots) or "none"
            return f"from {e.workflow}; slots cancelled: {ids}"
        case ev.SlotsCancelled():
            return f"slot(s) {and_list(str(i) for i in e.slots)} of {e.stage}"
        case ev.ItemFinalized(status="final"):
            ids = and_list(str(i) for i in e.label_ids)
            return f"from {e.source_stage} (label {ids}): {_short(canonical_json(e.label))}"
        case ev.ItemFinalized(status="unresolved"):
            ids = and_list(str(i) for i in e.label_ids)
            return f"unresolved: labels {ids} in {e.source_stage} disagreed"
        case ev.ItemFinalized():
            return "no final label"
    return e.type


def _short(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _duration(ms: int) -> str:
    seconds = round(ms / 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
