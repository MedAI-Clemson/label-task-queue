"""The engine: the commands people use, each turned into logged events.

One ``Engine`` serves one project (its roles, datasets and workflows). Every command runs in
one store transaction: it reads the current state, checks the request, and appends events;
the reducer applies each event to the derived tables as it is appended. A refused request
raises an ``EngineError`` with a plain-language message and appends nothing.

The engine is plain Python. It knows nothing about the web layer or the database.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from labelflow.engine import events as ev
from labelflow.engine.config import WorkflowConfig, role_warnings
from labelflow.engine.config.models import NAME_PATTERN
from labelflow.engine.edits import ChangePreview, ConfigSource, prepare_edit
from labelflow.engine.eligibility import Person, load_person, offered_last, why_not
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
    UnknownDataset,
    UnknownLabelTaskRef,
    UnknownRole,
    UnknownSlot,
    VersionConflict,
)
from labelflow.engine.events import Shown, canonical_json
from labelflow.engine.explain import Explanation, explain
from labelflow.engine.export import export_items, export_labels, sampling_report, score_report
from labelflow.engine.flow import Flow
from labelflow.engine.queries import (
    active_config,
    active_pause,
    and_list,
    blocking_pauses,
    config_at,
    did_you_mean,
    held_roles,
    item_row,
    labels_of,
    next_id,
    pause_text,
    role_holders,
    stage_of,
    workflow_item,
    workflow_row,
)
from labelflow.engine.store import Store
from labelflow.engine.tables import Row
from labelflow.engine.views import (
    ConsensusShown,
    Pause,
    PauseState,
    PriorLabel,
    Slot,
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

    # --- helpers --------------------------------------------------------------------------

    def _now(self) -> datetime:
        """The command's timestamp: one per command, in UTC."""
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("The engine clock must return timezone-aware datetimes.")
        return now.astimezone(UTC)

    def _flow(self, workflow: str, at: datetime, version: int | None = None) -> Flow:
        """The automatic decisions, following ``version`` (the one the command read)."""
        return Flow(self.store, self.label_tasks, workflow, at, version)

    def _my_slot(self, workflow: str, slot_id: int, user: str) -> Row:
        """The slot, if it belongs to the workflow and the person has claimed it."""
        workflow_row(self.store, workflow)
        slot = self.store.get("slots", slot=slot_id)
        if slot is None or slot["workflow"] != workflow:
            raise UnknownSlot(f"Workflow '{workflow}' has no slot {slot_id}.")
        if slot["status"] == "claimed" and slot["claimed_by"] == user:
            return slot
        state = {
            "open": "it is open; ask for work to claim a slot",
            "claimed": f"it is claimed by {slot['claimed_by']}",
            "filled": "it already has its label",
            "cancelled": "it was cancelled",
        }[slot["status"]]
        raise SlotNotYours(f"Slot {slot_id} is not claimed by {user}: {state}.")

    def _require_role(self, role: str) -> None:
        if self.store.get("roles", role=role) is None:
            known = [r["role"] for r in self.store.select("roles")]
            raise UnknownRole(
                f"There is no role named '{role}'; define it first.{did_you_mean(role, known)}"
            )

    def _require_dataset(self, dataset: str) -> None:
        if self.store.get("datasets", dataset=dataset) is None:
            known = [r["dataset"] for r in self.store.select("datasets")]
            raise UnknownDataset(
                f"There is no dataset named '{dataset}'.{did_you_mean(dataset, known)}"
            )

    # --- project: roles, datasets, items --------------------------------------------------

    def define_role(self, role: str, actor: str | None) -> None:
        """Define a role that workflows can assign stages to."""
        _check_name(role, "role")
        with self.store.transaction():
            if self.store.get("roles", role=role) is not None:
                raise AlreadyExists(f"Role '{role}' is already defined.")
            self.store.append(
                ev.RoleDefined(
                    at=self._now(), actor=actor, role=role, because=f"defined by {actor}"
                )
            )

    def grant_role(self, user: str, role: str, actor: str | None, reason: str) -> list[str]:
        """Give a person a role. Returns staffing warnings for the running workflows."""
        _check_text(user, "user")
        _check_text(reason, "reason")
        with self.store.transaction():
            self._require_role(role)
            if role in held_roles(self.store, user):
                raise AlreadyExists(f"{user} already holds role '{role}'.")
            self.store.append(
                ev.RoleGranted(
                    at=self._now(),
                    actor=actor,
                    user=user,
                    role=role,
                    reason=reason,
                    because=f"granted by {actor}: {reason}",
                )
            )
            return self._all_staffing_warnings()

    def revoke_role(self, user: str, role: str, actor: str | None, reason: str) -> list[str]:
        """Take a role away from a person. Claims they hold are kept; it applies at the next
        claim. Returns staffing warnings for the running workflows."""
        _check_text(reason, "reason")
        with self.store.transaction():
            self._require_role(role)
            if role not in held_roles(self.store, user):
                raise NotAllowed(f"{user} does not hold role '{role}'.")
            self.store.append(
                ev.RoleRevoked(
                    at=self._now(),
                    actor=actor,
                    user=user,
                    role=role,
                    reason=reason,
                    because=f"revoked by {actor}: {reason}",
                )
            )
            return self._all_staffing_warnings()

    def create_dataset(self, name: str, actor: str | None) -> None:
        _check_name(name, "dataset")
        with self.store.transaction():
            if self.store.get("datasets", dataset=name) is not None:
                raise AlreadyExists(f"Dataset '{name}' already exists.")
            self.store.append(
                ev.DatasetCreated(
                    at=self._now(), actor=actor, dataset=name, because=f"created by {actor}"
                )
            )

    def import_item(
        self,
        dataset: str,
        item_id: str,
        uri: str,
        content_hash: str,
        properties: Mapping[str, Any] | None = None,
        actor: str | None = None,
    ) -> None:
        """Add an item to a dataset. The caller hashes the image; the engine never reads it.

        The item enters every running workflow over the dataset in the same transaction.
        """
        _check_text(item_id, "item id")
        _check_text(uri, "uri")
        _check_text(content_hash, "content hash")
        try:
            props = _JSON_DICT.validate_python(dict(properties or {}))
        except ValidationError as error:
            raise InvalidRequest(f"Item properties must be JSON values: {error}") from None
        with self.store.transaction():
            self._require_dataset(dataset)
            if self.store.get("items", dataset=dataset, item=item_id) is not None:
                raise AlreadyExists(f"Dataset '{dataset}' already has an item '{item_id}'.")
            at = self._now()
            self.store.append(
                ev.ItemImported(
                    at=at,
                    actor=actor,
                    item=item_id,
                    dataset=dataset,
                    uri=uri,
                    content_hash=content_hash,
                    properties=props,
                    because=f"imported into {dataset} by {actor}",
                )
            )
            running = self.store.select("workflows", dataset=dataset, status="running")
            for row in sorted(running, key=lambda r: r["seq"]):
                because = (
                    f"{row['workflow']}.dataset = {dataset}; the item was imported while the "
                    "workflow runs"
                )
                self._flow(row["workflow"], at).enter(item_id, because)

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
            self._require_dataset(config.dataset)
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
            at = self._now()
            self.store.append(
                ev.WorkflowStarted(
                    at=at,
                    actor=actor,
                    workflow=name,
                    config_version=1,
                    dataset=config.dataset,
                    label_task=config.label_task,
                    seed=seed,
                    config=data,
                    config_hash=_config_hash(data),
                    because=f"started by {actor} with config version 1 and seed {seed}",
                )
            )
            flow = self._flow(name, at)
            items = self.store.select("items", dataset=config.dataset)
            for item in sorted(items, key=lambda r: r["seq"]):
                because = (
                    f"{name}.dataset = {config.dataset}; the item was in the dataset when the "
                    "workflow started"
                )
                flow.enter(item["item"], because)
            return self.staffing_warnings(name)

    def staffing_warnings(self, workflow: str) -> list[str]:
        """Stages that the people holding their roles cannot fill, in plain language."""
        with self.store.transaction():
            _, config = active_config(self.store, workflow)
            roles = role_holders(self.store)
            return [f"workflow {workflow}, {problem}" for problem in role_warnings(config, roles)]

    def _all_staffing_warnings(self) -> list[str]:
        rows = self.store.select("workflows", status="running")
        return [w for row in rows for w in self.staffing_warnings(row["workflow"])]

    # --- editing a running workflow -------------------------------------------------------

    def preview_edit(self, workflow: str, new_config: ConfigSource) -> ChangePreview:
        """What activating ``new_config`` would change, without changing anything.

        The proposal is a whole config document (a ``WorkflowConfig``, a mapping or YAML
        text). The preview classifies every change against the active version, lists why
        the edit would be refused (if it would), and counts what it touches. See ``edits``.
        """
        with self.store.transaction():
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
                when = datetime.fromisoformat(row["activated_at"]).strftime("%Y-%m-%d %H:%M UTC")
                raise VersionConflict(
                    f"Workflow '{workflow}' is at config version {active} (activated by "
                    f"{row['activated_by']} on {when}), but this edit was previewed against "
                    f"version {base_version}. Preview it again against version {active}, then "
                    "activate it."
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
            self.store.append(
                ev.ConfigActivated(
                    at=self._now(),
                    actor=actor,
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
            )
            return self.staffing_warnings(workflow)

    # --- labelers -------------------------------------------------------------------------

    def request_work(self, workflow: str, user: str) -> Slot | None:
        """Claim the next slot for this person, or return the one they already hold.

        A person holds at most one claim per workflow. The order: slots in later-listed
        stages first (so items finish), then the oldest admitted item, then slot index;
        except that slots of items the person released in that stage come after all the
        others. Slots of a paused stage or workflow are not handed out. Returns None when
        nothing is available to this person.
        """
        _check_text(user, "user")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            mine = self.store.select("slots", workflow=workflow, status="claimed", claimed_by=user)
            if mine:
                return Slot.from_row(mine[0])  # a pause does not take a held claim away
            person = load_person(self.store, workflow, user)
            if not person.roles or active_pause(self.store, workflow) is not None:
                return None
            position = {
                stage.name: i
                for i, stage in enumerate(config.stages)
                if active_pause(self.store, workflow, stage.name) is None
            }
            candidates = [
                slot
                for slot in self.store.select("slots", workflow=workflow, status="open")
                if slot["stage"] in position
                and why_not(person, slot, config.stage(slot["stage"]).roles) is None
            ]
            if not candidates:
                return None
            chosen = min(
                candidates,
                key=lambda s: (
                    offered_last(person, s),
                    -position[s["stage"]],
                    s["admitted_seq"],
                    s["index"],
                    s["slot"],
                ),
            )
            how = (
                "asked for work; first eligible open slot in the default order (later stages "
                "first, then the oldest admitted item)"
            )
            if offered_last(person, chosen):
                how += (
                    f"; {user} released this item in {chosen['stage']} before, so it was "
                    "offered only after their other work"
                )
            return self._claim(workflow, chosen, person, version, how)

    def claim(self, workflow: str, slot_id: int, user: str) -> Slot:
        """Claim one particular open slot, for an interface that lets people choose.

        Raises Paused when the slot's stage or workflow is paused, and NotEligible, with the
        reason, when the person may not claim it. A slot the person released before may be
        claimed again this way.
        """
        _check_text(user, "user")
        with self.store.transaction():
            version, config = active_config(self.store, workflow)
            slot = self.store.get("slots", slot=slot_id)
            if slot is None or slot["workflow"] != workflow:
                raise UnknownSlot(f"Workflow '{workflow}' has no slot {slot_id}.")
            mine = self.store.select("slots", workflow=workflow, status="claimed", claimed_by=user)
            if mine:
                raise NotAllowed(
                    f"{user} already holds slot {mine[0]['slot']} in {workflow}; submit or "
                    "release it first (one claim per workflow at a time)."
                )
            if slot["status"] != "open":
                raise NotAllowed(f"Slot {slot_id} is not open: it is {slot['status']}.")
            pauses = blocking_pauses(self.store, workflow, slot["stage"])
            if pauses:
                raise Paused(
                    f"Slot {slot_id} cannot be claimed now: {' and '.join(pauses)}. It can be "
                    "claimed once that is resumed."
                )
            person = load_person(self.store, workflow, user)
            reason = why_not(person, slot, stage_of(config, slot["stage"]).roles)
            if reason is not None:
                raise NotEligible(f"{user} may not claim slot {slot_id}: {reason}.")
            return self._claim(workflow, slot, person, version, "chose this slot")

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
        self.store.append(
            ev.SlotClaimed(
                at=self._now(),
                actor=user,
                workflow=workflow,
                stage=slot["stage"],
                item=slot["item"],
                config_version=version,
                slot=slot["slot"],
                user=user,
                roles=tuple(sorted(person.roles)),
                because=because,
            )
        )
        return Slot.from_row(self.store.get("slots", slot=slot["slot"]))

    def work_packet(self, workflow: str, slot_id: int, user: str) -> WorkPacket:
        """What the interface should show for a claimed slot.

        With ``show_prior`` (the default), that includes the item's labels from other stages
        of this workflow (ids and stages, not labeler names) and the most recent consensus
        check's detail. Item properties are never included.
        """
        with self.store.transaction():
            slot = self._my_slot(workflow, slot_id, user)
            _, config = active_config(self.store, workflow)
            stage = stage_of(config, slot["stage"])
            item = item_row(self.store, workflow, slot["item"])
            priors: tuple[PriorLabel, ...] = ()
            consensus = None
            if stage.show_prior:
                priors = tuple(
                    PriorLabel(label=r["label"], stage=r["stage"], value=r["value"])
                    for r in labels_of(self.store, workflow, slot["item"])
                    if r["stage"] != slot["stage"]
                )
                checks = self.store.select("consensus", workflow=workflow, item=slot["item"])
                if checks:
                    latest = max(checks, key=lambda r: r["seq"])
                    consensus = ConsensusShown(
                        stage=latest["stage"],
                        label_ids=tuple(latest["label_ids"]),
                        agreed=latest["agreed"],
                        rule_version=latest["rule_version"],
                        detail=latest["detail"],
                    )
            return WorkPacket(
                workflow=workflow,
                slot=slot_id,
                stage=slot["stage"],
                item=slot["item"],
                uri=item["uri"],
                content_hash=item["content_hash"],
                show_prior=stage.show_prior,
                prior_labels=priors,
                consensus=consensus,
            )

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
            if not isinstance(label, dict):
                raise InvalidLabel([f"a label must be a JSON object, not {type(label).__name__}"])
            try:
                label = _JSON_DICT.validate_python(label)
                task.validate(label)
            except ValidationError:
                raise InvalidLabel(["the label must contain only JSON values"]) from None
            except LabelValidationError as error:
                raise InvalidLabel(error.problems) from error
            self._check_shown(workflow, slot, self._could_show_prior(slot, version), shown)
            at = self._now()
            label_id = next_id(self.store, "label")
            self.store.append(
                ev.LabelSubmitted(
                    at=at,
                    actor=user,
                    workflow=workflow,
                    stage=slot["stage"],
                    item=slot["item"],
                    config_version=version,
                    slot=slot_id,
                    label_id=label_id,
                    user=user,
                    roles=tuple(sorted(held_roles(self.store, user))),
                    label=label,
                    shown=shown,
                    label_task=task.ref,
                    ontology_version=task.ontology_version,
                    duration_ms=duration_ms,
                    because=f"{user} submitted the label for slot {slot_id} of {slot['stage']}",
                )
            )
            flow = self._flow(workflow, at, version)
            if stage.score_against:
                flow.score(stage, slot["item"], label_id, user, label)
            flow.slots_closed(slot["stage"], slot["item"])
            return label_id

    def _could_show_prior(self, slot: Row, version: int) -> bool:
        """Whether the interface may have shown earlier labels for this claimed slot.

        That is so if the slot's stage shows them under the version in force now, or under
        any version since the slot was claimed: an edit that makes the stage blind must not
        reject a label whose author was legitimately shown earlier labels before it.
        """
        workflow, stage = slot["workflow"], slot["stage"]
        if config_at(self.store, workflow, version).stage(stage).show_prior:
            return True
        claims = [
            e
            for e in self.store.events(workflow=workflow, item=slot["item"])
            if isinstance(e, ev.SlotClaimed) and e.slot == slot["slot"]
        ]
        since = claims[-1].config_version if claims else version
        for earlier in range(since or version, version):
            config = config_at(self.store, workflow, earlier)
            if stage in config.stage_names and config.stage(stage).show_prior:
                return True
        return False

    def _check_shown(self, workflow: str, slot: Row, stage_shows_prior: bool, shown: Shown) -> None:
        """Check that what the interface reports showing fits the item and its labels."""
        item = slot["item"]
        content_hash = item_row(self.store, workflow, item)["content_hash"]
        if shown.content_hash != content_hash:
            raise InvalidShown(
                f"The interface reports showing content {shown.content_hash}, but item {item} "
                f"has content hash {content_hash}."
            )
        if shown.prior_label_ids:
            if not shown.show_prior:
                raise InvalidShown("Earlier labels are reported as shown, but show_prior is false.")
            if not stage_shows_prior:
                raise InvalidShown(
                    f"Stage {slot['stage']} is blind (show_prior: false), but the interface "
                    "reports showing earlier labels."
                )
            known = {r["label"] for r in labels_of(self.store, workflow, item)}
            strangers = [i for i in shown.prior_label_ids if i not in known]
            if strangers:
                raise InvalidShown(
                    f"Label(s) {and_list(map(str, strangers))} are not labels of item {item} in "
                    f"workflow {workflow}."
                )
        if shown.consensus_rule_version is not None:
            rules = {
                r["rule_version"]
                for r in self.store.select("consensus", workflow=workflow, item=item)
            }
            if shown.consensus_rule_version not in rules:
                raise InvalidShown(
                    f"No consensus check on item {item} used rule {shown.consensus_rule_version}."
                )

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
            self.store.append(
                ev.SlotReleased(
                    at=self._now(),
                    actor=user,
                    workflow=workflow,
                    stage=slot["stage"],
                    item=slot["item"],
                    config_version=version,
                    slot=slot_id,
                    user=user,
                    reason=reason,
                    because=f"released by {user}: {reason}",
                )
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
            self.store.append(
                ev.ItemExcluded(
                    at=self._now(),
                    actor=actor,
                    workflow=workflow,
                    item=item,
                    config_version=version,
                    reason=reason,
                    cancelled_slots=cancelled,
                    because=because,
                )
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
            open_slots = self.store.select("slots", workflow=workflow, stage=stage, status="open")
            by_item: dict[str, list[Row]] = {}
            for slot in sorted(open_slots, key=lambda s: (s["admitted_seq"], s["index"])):
                by_item.setdefault(slot["item"], []).append(slot)
            at = self._now()
            flow = self._flow(workflow, at, version)
            for item, slots in by_item.items():
                self.store.append(
                    ev.SlotsCancelled(
                        at=at,
                        actor=actor,
                        workflow=workflow,
                        stage=stage,
                        item=item,
                        config_version=version,
                        reason=reason,
                        slots=tuple(s["slot"] for s in slots),
                        because=f"cancelled by {actor}: {reason}",
                    )
                )
                flow.slots_closed(stage, item)
            return [s["slot"] for slots in by_item.values() for s in slots]

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
                because = f"resumed by {actor}: {reason}; " + self._still_paused(
                    workflow, stage, config.stage_names, what
                )
            self.store.append(
                kind(
                    at=self._now(),
                    actor=actor,
                    workflow=workflow,
                    stage=stage,
                    config_version=version,
                    reason=reason,
                    because=because,
                )
            )

    def _still_paused(
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

    # --- reads ----------------------------------------------------------------------------

    def pauses(self, workflow: str) -> PauseState:
        """What is paused in the workflow now: the workflow itself, and each stage."""
        with self.store.transaction():
            _, config = active_config(self.store, workflow)
            return PauseState(
                workflow=workflow,
                paused=Pause.from_row(active_pause(self.store, workflow)),
                stages={
                    name: Pause.from_row(active_pause(self.store, workflow, name))
                    for name in config.stage_names
                },
            )

    def slot(self, workflow: str, slot_id: int) -> Slot:
        with self.store.transaction():
            workflow_row(self.store, workflow)
            row = self.store.get("slots", slot=slot_id)
            if row is None or row["workflow"] != workflow:
                raise UnknownSlot(f"Workflow '{workflow}' has no slot {slot_id}.")
            return Slot.from_row(row)

    def explain(self, workflow: str, item: str) -> Explanation:
        """Why the item went where it went, and why it is waiting, if it is."""
        with self.store.transaction():
            return explain(self.store, workflow, item)

    def export_items(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            return export_items(self.store, workflow)

    def export_labels(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            return export_labels(self.store, workflow)

    def score_report(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            return score_report(self.store, workflow)

    def sampling_report(self, workflow: str) -> list[dict[str, Any]]:
        with self.store.transaction():
            return sampling_report(self.store, workflow)


def _config_hash(data: Mapping[str, Any]) -> str:
    """The fingerprint of a config version: the SHA-256 of its canonical JSON."""
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def _check_text(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"A {what} is required (a non-empty string).")


def _check_name(value: object, what: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(NAME_PATTERN, value):
        raise InvalidRequest(
            f"'{value}' is not a valid {what} name: use letters, digits and underscores, "
            "starting with a letter."
        )
