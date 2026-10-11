"""The engine: the commands people use, each turned into logged events, and the reads.

One ``Engine`` serves one project (its roles, datasets and workflows). Every command runs in
one store transaction: it reads the current state, checks the request, and appends events;
the reducer applies each event to the derived tables as it is appended. A refused request
raises an ``EngineError`` with a plain-language message and appends nothing. Every read
runs in ``store.reading()``: one consistent state, no write lock.

The engine is plain Python. It knows nothing about the web layer or the database.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from labelflow.engine import events as ev
from labelflow.engine import reads
from labelflow.engine.config import ValidationContext, WorkflowConfig, and_list, role_warnings
from labelflow.engine.config.models import NAME_PATTERN
from labelflow.engine.edits import ChangePreview, ConfigSource, prepare_edit
from labelflow.engine.eligibility import Person, load_person, next_slot, why_not
from labelflow.engine.errors import (
    AlreadyExists,
    EditRefused,
    InvalidLabel,
    InvalidRequest,
    InvalidShown,
    NotAllowed,
    NotEligible,
    Paused,
    SlotNotYours,
    UnknownLabelTaskRef,
    UnknownRole,
    VersionConflict,
)
from labelflow.engine.events import Event, Shown, canonical_json
from labelflow.engine.explain import Explanation, explain
from labelflow.engine.export import export_items, export_labels, sampling_report, score_report
from labelflow.engine.flow import Flow, ask_task
from labelflow.engine.queries import (
    active_config,
    active_pause,
    blocking_pauses,
    config_at,
    dataset_row,
    held_roles,
    next_id,
    pause_text,
    role_holders,
    role_row,
    slot_row,
    stage_of,
    when,
    workflow_item,
    workflow_row,
)
from labelflow.engine.shown import check_shown
from labelflow.engine.store import Store
from labelflow.engine.tables import Row
from labelflow.engine.views import (
    ConfigVersion,
    Dataset,
    Item,
    NewItem,
    Page,
    PauseState,
    Progress,
    Role,
    Slot,
    Workflow,
    WorkflowDetail,
    WorkPacket,
)
from labelflow.labeltask import Label, LabelTaskRegistry, LabelValidationError, UnknownLabelTask

Clock = Callable[[], datetime]

_JSON_DICT = TypeAdapter(dict[str, JsonValue])


def utc_now() -> datetime:
    return datetime.now(UTC)


class Engine:
    """A project's labeling workflows: roles, datasets, items, workflows, slots and labels."""

    def __init__(self, store: Store, label_tasks: LabelTaskRegistry, clock: Clock = utc_now):
        self.store = store
        self.label_tasks = label_tasks
        self.clock = clock

    def _now(self) -> datetime:
        """The command's timestamp: one per command, in UTC."""
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("The engine clock must return timezone-aware datetimes.")
        return now.astimezone(UTC)

    def _flow(self, workflow: str, at: datetime, version: int | None = None) -> Flow:
        """The automatic decisions, following ``version`` (the one the command read)."""
        return Flow(self.store, self.label_tasks, workflow, at, version)

    # --- project: roles, datasets, items --------------------------------------------------

    def define_role(self, role: str, actor: str | None) -> None:
        """Define a role that workflows can assign stages to."""
        _check_name(role, "role")
        with self.store.transaction():
            if self.store.get("roles", role=role) is not None:
                raise AlreadyExists(f"Role '{role}' is already defined.")
            self._log(ev.RoleDefined, actor, role=role, because=f"defined by {actor}")

    def grant_role(self, user: str, role: str, actor: str | None, reason: str) -> list[str]:
        """Give a person a role. Returns staffing warnings for the running workflows."""
        _check_text(user, "user")
        _check_text(reason, "reason")
        with self.store.transaction():
            role_row(self.store, role)
            if role in held_roles(self.store, user):
                raise AlreadyExists(f"{user} already holds role '{role}'.")
            because = f"granted by {actor}: {reason}"
            self._log(ev.RoleGranted, actor, user=user, role=role, reason=reason, because=because)
            return self._all_staffing_warnings()

    def revoke_role(self, user: str, role: str, actor: str | None, reason: str) -> list[str]:
        """Take a role away from a person. Claims they hold are kept; it applies at the next
        claim. Returns staffing warnings for the running workflows."""
        _check_text(reason, "reason")
        with self.store.transaction():
            role_row(self.store, role)
            if role not in held_roles(self.store, user):
                raise NotAllowed(f"{user} does not hold role '{role}'.")
            because = f"revoked by {actor}: {reason}"
            self._log(ev.RoleRevoked, actor, user=user, role=role, reason=reason, because=because)
            return self._all_staffing_warnings()

    def create_dataset(self, name: str, actor: str | None) -> None:
        _check_name(name, "dataset")
        with self.store.transaction():
            if self.store.get("datasets", dataset=name) is not None:
                raise AlreadyExists(f"Dataset '{name}' already exists.")
            self._log(ev.DatasetCreated, actor, dataset=name, because=f"created by {actor}")

    def import_item(
        self,
        dataset: str,
        item_id: str,
        uri: str,
        content_hash: str,
        properties: Mapping[str, Any] | None = None,
        actor: str | None = None,
    ) -> None:
        """Add one item to a dataset (see ``import_items``)."""
        try:
            props = dict(properties or {})
            new = NewItem(item=item_id, uri=uri, content_hash=content_hash, properties=props)
        except (TypeError, ValidationError) as error:
            raise InvalidRequest(
                f"The item needs text fields and JSON properties: {error}"
            ) from None
        self.import_items(dataset, [new], actor)

    def import_items(self, dataset: str, items: Iterable[NewItem], actor: str | None) -> list[str]:
        """Add items to a dataset, all or none; returns their ids in import order.

        The caller hashes each image; the engine never reads it. An item already in the
        dataset, or listed twice, refuses the whole batch. Each item enters every running
        workflow over the dataset in the same transaction.
        """
        batch = list(items)
        with self.store.transaction():
            dataset_row(self.store, dataset)
            seen: set[str] = set()
            for new in batch:
                _check_text(new.item, "item id")
                _check_text(new.uri, "uri")
                _check_text(new.content_hash, "content hash")
                if self.store.get("items", dataset=dataset, item=new.item) is not None:
                    raise AlreadyExists(f"Dataset '{dataset}' already has an item '{new.item}'.")
                if new.item in seen:
                    raise InvalidRequest(f"Item '{new.item}' is listed twice in the import.")
                seen.add(new.item)
            at = self._now()
            running = self.store.select("workflows", dataset=dataset, status="running")
            flows = [self._flow(r["workflow"], at) for r in sorted(running, key=lambda r: r["seq"])]
            for new in batch:
                self._log(
                    ev.ItemImported,
                    actor,
                    at=at,
                    dataset=dataset,
                    because=f"imported into {dataset} by {actor}",
                    **new.model_dump(),
                )
                for flow in flows:
                    flow.enter(new.item, "the item was imported while the workflow runs")
            return [new.item for new in batch]

    # --- workflows ------------------------------------------------------------------------

    def start_workflow(
        self, config: WorkflowConfig | Mapping[str, Any], seed: int, actor: str | None
    ) -> list[str]:
        """Start a workflow with config version 1 and admit every item of its dataset.

        Items enter in import order. Returns staffing warnings (stages whose roles are too
        small to fill them).
        """
        if not isinstance(config, WorkflowConfig):
            config = WorkflowConfig.model_validate(config)
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**63:
            # 2**63: the seed is stored in a 64-bit signed integer column
            raise InvalidRequest("The seed must be a whole number from 0 to 2**63 - 1.")
        with self.store.transaction():
            name = config.workflow
            if self.store.get("workflows", workflow=name) is not None:
                raise AlreadyExists(f"A workflow named '{name}' already exists.")
            dataset_row(self.store, config.dataset)
            try:
                self.label_tasks.get(config.label_task)
            except UnknownLabelTask as error:
                raise UnknownLabelTaskRef(str(error)) from None
            undefined = sorted(
                {r for s in config.stages for r in s.roles if not self.store.get("roles", role=r)}
            )
            if undefined:
                raise UnknownRole(
                    f"The config assigns stages to undefined role(s) {and_list(undefined)}; "
                    "define them first."
                )
            data = config.model_dump(mode="json")
            at = self._log(
                ev.WorkflowStarted,
                actor,
                workflow=name,
                config_version=1,
                dataset=config.dataset,
                label_task=config.label_task,
                seed=seed,
                config=data,
                config_hash=_config_hash(data),
                because=f"started by {actor} with config version 1 and seed {seed}",
            ).at
            flow = self._flow(name, at)
            for item in sorted(self.store.select("items", dataset=config.dataset), key=_seq):
                flow.enter(item["item"], "the item was in the dataset when the workflow started")
            return self.staffing_warnings(name)

    def preview_edit(self, workflow: str, new_config: ConfigSource) -> ChangePreview:
        """What activating ``new_config`` would change, without changing anything.

        The proposal is a whole config document (a ``WorkflowConfig``, a mapping or YAML
        text). The preview classifies every change against the active version, lists why
        the edit would be refused (if it would), and counts what it touches. See ``edits``.
        """
        with self.store.reading():
            preview, _ = prepare_edit(self.store, workflow, new_config)
            return preview

    def activate_edit(
        self,
        workflow: str,
        new_config: ConfigSource,
        actor: str | None,
        base_version: int,
        reason: str = "",
    ) -> list[str]:
        """Make ``new_config`` the workflow's next config version (a forward-only edit).

        ``base_version`` is the version the change was previewed against: if another edit
        was activated since, this one is refused (VersionConflict), so nobody activates a
        change they have not seen the preview of. An edit the preview refuses raises
        EditRefused with every reason. Otherwise one ``config_activated`` event is logged,
        carrying the new config, the diff and the preview's counts, and nothing else changes:
        the new version governs only decisions made from now on.

        Returns staffing warnings for the new version, as ``start_workflow`` does.
        """
        if not isinstance(base_version, int) or isinstance(base_version, bool):
            raise InvalidRequest(
                "base_version must be the version number the edit was previewed against."
            )
        if not isinstance(reason, str):
            raise InvalidRequest("The reason must be text (it may be empty).")
        with self.store.transaction():
            active, _ = active_config(self.store, workflow)
            if base_version != active:
                row = self.store.get("workflow_versions", workflow=workflow, version=active)
                raise VersionConflict(
                    f"Workflow '{workflow}' is at config version {active} (activated by "
                    f"{row['activated_by']} on {when(row['activated_at'])}), but this edit was "
                    f"previewed against version {base_version}. Preview it again against "
                    f"version {active}, then activate it."
                )
            preview, config = prepare_edit(self.store, workflow, new_config)
            if not preview.ok or config is None:
                raise EditRefused(preview.errors)
            data = config.model_dump(mode="json")
            because = (
                f"activated by {actor}: config version {preview.new_version} replaces version "
                f"{active}"
            )
            if reason.strip():
                because += f"; {reason}"
            self._log(
                ev.ConfigActivated,
                actor,
                workflow=workflow,
                config_version=preview.new_version,
                base_version=active,
                config=data,
                config_hash=_config_hash(data),
                changes=tuple(change.model_dump(mode="json") for change in preview.changes),
                diff=preview.diff,
                counts=preview.counts(),
                warnings=preview.warnings,
                reason=reason,
                because=because,
            )
            return self.staffing_warnings(workflow)

    # --- labelers -------------------------------------------------------------------------

    def request_work(self, workflow: str, user: str) -> Slot | None:
        """Claim the next slot for this person, or return the one they already hold.

        A person holds at most one claim per workflow. The order is the default order of
        ``eligibility``; slots of a paused stage or workflow are not handed out. Returns
        None when nothing is available to this person.
        """
        _check_text(user, "user")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            held = self._held_claim(workflow, user)
            if held is not None:
                return Slot.from_row(held)  # a pause does not take a held claim away
            person = load_person(self.store, workflow, user)
            found = next_slot(self.store, workflow, config, person)
            if found is None:
                return None
            slot, how = found
            return self._claim(workflow, slot, person, version, how)

    def claim(self, workflow: str, slot_id: int, user: str) -> Slot:
        """Claim one particular open slot, for an interface that lets people choose.

        Raises Paused when the slot's stage or workflow is paused, and NotEligible, with the
        reason, when the person may not claim it. A slot the person released before may be
        claimed again this way.
        """
        _check_text(user, "user")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            slot = slot_row(self.store, workflow, slot_id)
            held = self._held_claim(workflow, user)
            if held is not None:
                raise NotAllowed(
                    f"{user} already holds slot {held['slot']} in {workflow}; submit or "
                    "release it first (one claim per workflow at a time)."
                )
            if slot["status"] != "open":
                raise NotAllowed(f"Slot {slot_id} is not open: it is {slot['status']}.")
            if pauses := blocking_pauses(self.store, workflow, slot["stage"]):
                raise Paused(
                    f"Slot {slot_id} cannot be claimed now: {' and '.join(pauses)}. It can be "
                    "claimed once that is resumed."
                )
            person = load_person(self.store, workflow, user)
            reason = why_not(person, slot, stage_of(config, slot["stage"]).roles)
            if reason is not None:
                raise NotEligible(f"{user} may not claim slot {slot_id}: {reason}.")
            return self._claim(workflow, slot, person, version, "chose this slot")

    def _held_claim(self, workflow: str, user: str) -> Row | None:
        mine = self.store.select("slots", workflow=workflow, status="claimed", claimed_by=user)
        return mine[0] if mine else None

    def _claim(self, workflow: str, slot: Row, person: Person, version: int, how: str) -> Slot:
        """Log the claim; ``version`` is the config version in force (its roles applied)."""
        stage = config_at(self.store, workflow, version).stage(slot["stage"])
        user = person.user
        via = sorted(person.roles & set(stage.roles))
        because = (
            f"{user} {how}; {stage.name}.assign_to = {and_list(stage.roles, 'or')} and "
            f"{user} holds {and_list(via)}"
        )
        if slot["reserved_for"]:
            because += f"; reserved for {user} by {stage.name}.labels_per_item = all"
        self._log(
            ev.SlotClaimed,
            user,
            workflow=workflow,
            stage=slot["stage"],
            item=slot["item"],
            config_version=version,
            slot=slot["slot"],
            user=user,
            roles=tuple(sorted(person.roles)),
            because=because,
        )
        return Slot.from_row(self.store.get("slots", slot=slot["slot"]))

    def _my_slot(self, workflow: str, slot_id: int, user: str) -> Row:
        """The slot, if the person has claimed it."""
        slot = slot_row(self.store, workflow, slot_id)
        if slot["status"] == "claimed" and slot["claimed_by"] == user:
            return slot
        state = {
            "open": "it is open; ask for work to claim a slot",
            "claimed": f"it is claimed by {slot['claimed_by']}",
            "filled": "it already has its label",
            "cancelled": "it was cancelled",
        }[slot["status"]]
        raise SlotNotYours(f"Slot {slot_id} is not claimed by {user}: {state}.")

    def work_packet(self, workflow: str, slot_id: int, user: str) -> WorkPacket:
        """What the interface should show for a claimed slot (see ``reads.work_packet``)."""
        with self.store.reading():
            slot = self._my_slot(workflow, slot_id, user)
            _, config = active_config(self.store, workflow)
            return reads.work_packet(self.store, slot, stage_of(config, slot["stage"]))

    def submit(
        self,
        workflow: str,
        slot_id: int,
        user: str,
        label: Label,
        shown: Shown | Mapping[str, Any],
        duration_ms: int | None = None,
    ) -> int:
        """Submit the label for a claimed slot; returns the new label's id.

        The label task validates the label first; an invalid label is rejected with no
        event. ``shown`` lists what the interface showed (see ``WorkPacket.shown``) and is
        checked against the item and its labels.
        """
        if duration_ms is not None and (not isinstance(duration_ms, int) or duration_ms < 0):
            raise InvalidRequest("duration_ms must be a whole number of milliseconds, or None.")
        try:
            shown = shown if isinstance(shown, Shown) else Shown.model_validate(shown)
        except ValidationError as error:
            raise InvalidShown(f"The 'shown' references are malformed: {error}") from None
        with self.store.transaction():
            slot = self._my_slot(workflow, slot_id, user)
            version, config = active_config(self.store, workflow)
            stage = stage_of(config, slot["stage"])
            task = self.label_tasks.get(workflow_row(self.store, workflow)["label_task"])
            label = _checked_label(task, label)
            check_shown(self.store, slot, version, shown)
            label_id = next_id(self.store, "label")
            at = self._log(
                ev.LabelSubmitted,
                user,
                workflow=workflow,
                stage=slot["stage"],
                item=slot["item"],
                config_version=version,
                slot=slot_id,
                label_id=label_id,
                user=user,
                roles=tuple(sorted(held_roles(self.store, user))),  # at the submit; the
                # roles held at the claim come from the slot (labels.roles_at_claim)
                label=label,
                shown=shown,
                label_task=task.ref,
                ontology_version=task.ontology_version,
                duration_ms=duration_ms,
                because=f"{user} submitted the label for slot {slot_id} of {slot['stage']}",
            ).at
            flow = self._flow(workflow, at, version)
            if stage.score_against:
                flow.score(stage, slot["item"], label_id, user, label)
            flow.slots_closed(slot["stage"], slot["item"])
            return label_id

    def release(self, workflow: str, slot_id: int, user: str, reason: str) -> None:
        """Give a claimed slot back to the pool, with a reason.

        The person may claim it again, but ``request_work`` offers it to them only after all
        their other work (see ``eligibility``). A release is allowed while the stage or the
        workflow is paused.
        """
        _check_text(reason, "reason")
        with self.store.transaction():
            slot = self._my_slot(workflow, slot_id, user)
            version, _ = active_config(self.store, workflow)
            self._log(
                ev.SlotReleased,
                user,
                workflow=workflow,
                stage=slot["stage"],
                item=slot["item"],
                config_version=version,
                slot=slot_id,
                user=user,
                reason=reason,
                because=f"released by {user}: {reason}",
            )

    # --- admins ---------------------------------------------------------------------------

    def exclude_item(self, workflow: str, item: str, actor: str | None, reason: str) -> list[int]:
        """Take an item out of this workflow, for example an image that cannot be labeled.

        Its open and claimed slots are cancelled, and it gets status excluded. An item still
        in progress gets no final label. An item that already has its final label may be
        excluded too, for example when the image turns out to be wrong: its final-label
        record and its labels are kept as history, but its status is excluded from then on.
        Returns the cancelled slot ids.
        """
        _check_text(reason, "reason")
        with self.store.transaction():
            row = workflow_item(self.store, workflow, item)
            if row["status"] == "excluded":
                raise NotAllowed(f"Item {item} is already excluded from {workflow}.")
            version, _ = active_config(self.store, workflow)
            cancelled = tuple(
                r["slot"]
                for r in self.store.select("slots", workflow=workflow, item=item)
                if r["status"] in ("open", "claimed")
            )
            because = f"excluded by {actor}: {reason}"
            if row["status"] == "done":
                because += "; its earlier final label is kept as history"
            self._log(
                ev.ItemExcluded,
                actor,
                workflow=workflow,
                item=item,
                config_version=version,
                reason=reason,
                cancelled_slots=cancelled,
                because=because,
            )
            return list(cancelled)

    def cancel_open_slots(
        self, workflow: str, stage: str, actor: str | None, reason: str
    ) -> list[int]:
        """Cancel every open (unclaimed) slot of a stage; returns their ids.

        Items whose last slots in the stage are cancelled leave it at once, which may admit
        them downstream or finalize them; all of it is logged.
        """
        _check_text(reason, "reason")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            stage_of(config, stage)
            by_item: dict[str, list[int]] = {}
            open_slots = self.store.select("slots", workflow=workflow, stage=stage, status="open")
            for slot in sorted(open_slots, key=lambda s: (s["admitted_seq"], s["index"])):
                by_item.setdefault(slot["item"], []).append(slot["slot"])
            at = self._now()
            flow = self._flow(workflow, at, version)
            for item, slots in by_item.items():
                self._log(
                    ev.SlotsCancelled,
                    actor,
                    at=at,
                    workflow=workflow,
                    stage=stage,
                    item=item,
                    config_version=version,
                    reason=reason,
                    slots=tuple(slots),
                    because=f"cancelled by {actor}: {reason}",
                )
                flow.slots_closed(stage, item)
            return [slot for slots in by_item.values() for slot in slots]

    def pause_workflow(self, workflow: str, actor: str | None, reason: str) -> None:
        """Stop all claims in the workflow until it is resumed.

        A pause stops claims only: items keep arriving and being admitted (their slots open
        and wait), claims already held can still be submitted or released, and consensus,
        downstream admission and finalization go on as usual.
        """
        self._pause(workflow, None, actor, reason, pause=True)

    def resume_workflow(self, workflow: str, actor: str | None, reason: str) -> None:
        """Allow claims in the workflow again. Stages paused on their own stay paused."""
        self._pause(workflow, None, actor, reason, pause=False)

    def pause_stage(self, workflow: str, stage: str, actor: str | None, reason: str) -> None:
        """Stop claims in one stage until it is resumed (see ``pause_workflow``). The pause
        stays across config versions while the stage exists."""
        self._pause(workflow, stage, actor, reason, pause=True)

    def resume_stage(self, workflow: str, stage: str, actor: str | None, reason: str) -> None:
        """Allow claims in a paused stage again (unless the whole workflow is paused)."""
        self._pause(workflow, stage, actor, reason, pause=False)

    def _pause(
        self, workflow: str, stage: str | None, actor: str | None, reason: str, pause: bool
    ) -> None:
        """Log a pause or a resume of a workflow (``stage`` None) or of one of its stages."""
        _check_text(reason, "reason")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            what = f"workflow {workflow}"
            if stage is not None:
                stage_of(config, stage)
                what = f"stage {stage} of {workflow}"
            current = active_pause(self.store, workflow, stage)
            if pause and current is not None:
                raise NotAllowed(f"The {what} is already paused ({pause_text(current)}).")
            if not pause and current is None:
                raise NotAllowed(f"The {what} is not paused, so there is nothing to resume.")
            if pause:
                kind = ev.StagePaused if stage else ev.WorkflowPaused
                because = f"paused by {actor}: {reason}; no slot of the {what} can be claimed"
            else:
                kind = ev.StageResumed if stage else ev.WorkflowResumed
                opened = self._resume_text(workflow, stage, config.stage_names, what)
                because = f"resumed by {actor}: {reason}; {opened}"
            self._log(
                kind,
                actor,
                workflow=workflow,
                stage=stage,
                config_version=version,
                reason=reason,
                because=because,
            )

    def _resume_text(
        self, workflow: str, stage: str | None, stages: tuple[str, ...], what: str
    ) -> str:
        """What a resume opens up, given that the other kind of pause stays as it is."""
        if stage is not None:
            if active_pause(self.store, workflow) is not None:
                return f"its slots can be claimed once workflow {workflow} is resumed too"
            return f"slots of the {what} can be claimed again"
        paused = [name for name in stages if active_pause(self.store, workflow, name)]
        if paused:
            return f"slots can be claimed again, except in paused stage(s) {and_list(paused)}"
        return f"slots of the {what} can be claimed again"

    def _log(self, kind: type[Event], actor: str | None, **fields: Any) -> Event:
        """Append one event of this command, stamped with the command's time unless given."""
        fields.setdefault("at", self._now())
        return self.store.append(kind(actor=actor, **fields))

    # --- reads: the project ---------------------------------------------------------------

    def roles(self) -> list[Role]:
        """Every role, in the order defined, with who holds it now."""
        with self.store.reading():
            return reads.roles(self.store)

    def role(self, name: str) -> Role:
        with self.store.reading():
            return reads.role(self.store, name)

    def user_roles(self, user: str) -> set[str]:
        """The roles the person holds now."""
        with self.store.reading():
            return held_roles(self.store, user)

    def datasets(self) -> list[Dataset]:
        """Every dataset, in the order created, with its number of items."""
        with self.store.reading():
            return reads.datasets(self.store)

    def dataset(self, name: str) -> Dataset:
        with self.store.reading():
            return reads.dataset(self.store, name)

    def items(self, dataset: str, offset: int = 0, limit: int = 100) -> Page[Item]:
        """One page of a dataset's items, in import order."""
        with self.store.reading():
            return reads.items(self.store, dataset, offset, limit)

    def validation_context(self) -> ValidationContext:
        """What a new config is checked against: the roles and their holders, the datasets
        and the registered label tasks."""
        with self.store.reading():
            return ValidationContext(
                roles=role_holders(self.store),
                datasets=[row["dataset"] for row in self.store.select("datasets")],
                label_tasks=self.label_tasks.refs(),
            )

    def events(
        self,
        offset: int = 0,
        limit: int = 100,
        *,
        workflow: str | None = None,
        item: str | None = None,
        type: str | None = None,
    ) -> Page[Event]:
        """One page of the project's event log, oldest first (the audit view)."""
        with self.store.reading():
            return reads.event_page(self.store, offset, limit, workflow, item, type)

    # --- reads: workflows -----------------------------------------------------------------

    def workflows(self) -> list[Workflow]:
        """Every workflow, in the order started, with its stages and what is paused."""
        with self.store.reading():
            return reads.workflows(self.store)

    def workflow(self, name: str) -> WorkflowDetail:
        """A workflow as it runs now: its config in force, progress and pauses."""
        with self.store.reading():
            return reads.workflow(self.store, name)

    def progress(self, workflow: str) -> Progress:
        """Items by status and slots by stage."""
        with self.store.reading():
            return reads.progress(self.store, workflow)

    def pauses(self, workflow: str) -> PauseState:
        """What is paused in the workflow now: the workflow itself, and each stage."""
        with self.store.reading():
            return reads.pauses(self.store, workflow)

    def config_versions(self, workflow: str) -> list[ConfigVersion]:
        """Every config version of the workflow, oldest first, with who activated it and why."""
        with self.store.reading():
            return reads.config_versions(self.store, workflow)

    def staffing_warnings(self, workflow: str) -> list[str]:
        """Stages that the people holding their roles cannot fill, in plain language."""
        with self.store.reading():
            _, config = active_config(self.store, workflow)
            holders = role_holders(self.store)
            return [f"workflow {workflow}, {problem}" for problem in role_warnings(config, holders)]

    def _all_staffing_warnings(self) -> list[str]:
        running = self.store.select("workflows", status="running")
        return [w for row in running for w in self.staffing_warnings(row["workflow"])]

    def slot(self, workflow: str, slot_id: int) -> Slot:
        with self.store.reading():
            return Slot.from_row(slot_row(self.store, workflow, slot_id))

    def explain(self, workflow: str, item: str) -> Explanation:
        """Why the item went where it went, and why it is waiting, if it is."""
        with self.store.reading():
            return explain(self.store, workflow, item)

    def export_items(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.reading():
            return export_items(self.store, workflow)

    def export_labels(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.reading():
            return export_labels(self.store, workflow)

    def score_report(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.reading():
            return score_report(self.store, workflow)

    def sampling_report(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.reading():
            return sampling_report(self.store, workflow)


def _checked_label(task: Any, label: Any) -> dict[str, Any]:
    """The label in the task's normal form, or InvalidLabel with every problem."""
    if not isinstance(label, dict):
        raise InvalidLabel([f"a label must be a JSON object, not {type(label).__name__}"])
    try:
        plain = _JSON_DICT.validate_python(label)
    except ValidationError:
        raise InvalidLabel(["the label must contain only JSON values"]) from None

    def validate() -> dict[str, Any]:
        try:
            return task.validate(plain)
        except LabelValidationError as error:
            raise InvalidLabel(error.problems) from error

    return ask_task(task, "validating the label", validate)


def _config_hash(data: Mapping[str, Any]) -> str:
    """The fingerprint of a config version: the SHA-256 of its canonical JSON."""
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def _seq(row: Row) -> int:
    return row["seq"]


def _check_text(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"A {what} is required (a non-empty string).")


def _check_name(value: object, what: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(NAME_PATTERN, value):
        raise InvalidRequest(
            f"'{value}' is not a valid {what} name: use letters, digits and underscores, "
            "starting with a letter."
        )
