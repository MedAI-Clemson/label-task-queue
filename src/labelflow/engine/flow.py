"""What the workflow decides by itself as an item moves through it.

People claim, submit, release, exclude and cancel; the engine turns each of those into
events. Everything that follows automatically lives here:

- **Admission.** When an item arrives at a stage, the stage's ``sample`` decides whether it
  enters, and ``labels_per_item`` decides how many slots open.
- **Leaving a stage.** When none of the item's slots in a stage is open or claimed, the item
  leaves it. With two or more labels the label task checks consensus. The item's outputs
  (``annotate``, plus ``annotate.agreed`` or ``annotate.disagreed``) decide which downstream
  stages it arrives at next, at once.
- **The final step.** When the item has no open or claimed slot anywhere in the workflow,
  it is done, and its one final label for this workflow is created.

Every decision is one logged event whose ``because`` names the config line behind it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from labelflow.engine import events as ev
from labelflow.engine.config import LabelsFraction, Stage, and_list
from labelflow.engine.draws import draw
from labelflow.engine.errors import EngineError, LabelTaskFailed, OntologyChanged
from labelflow.engine.queries import (
    active_config,
    config_at,
    holders,
    labels_of,
    live_slots,
    next_id,
    workflow_row,
)
from labelflow.engine.store import Store
from labelflow.labeltask import ConsensusResult, LabelTaskRegistry


def ask_task(task: Any, doing: str, call: Callable[[], Any]) -> Any:
    """Run one label task call; an unexpected error becomes a LabelTaskFailed.

    The label task is code outside the engine. If it raises anything but the errors the
    caller expects, the command is refused with a plain message instead of crashing, so the
    transaction rolls back and nothing half-done is recorded.
    """
    try:
        return call()
    except EngineError:
        raise  # a refusal the caller already phrased (an invalid label, for example)
    except Exception as error:
        raise LabelTaskFailed(
            f"Label task {task.ref} failed while {doing}: {type(error).__name__}: {error}. "
            "This is a problem in the label task, not in the request; nothing was recorded."
        ) from error


class Flow:
    """The automatic decisions for one workflow, within one command (one transaction).

    Every decision follows one config version: the version in force when the command
    started, which the command passes in (or, if it does not, the active one). An edit
    activated later governs only later commands; that is what makes edits forward-only.
    """

    def __init__(
        self,
        store: Store,
        label_tasks: LabelTaskRegistry,
        workflow: str,
        at: datetime,
        version: int | None = None,
    ):
        self.store = store
        self.workflow = workflow
        self.at = at
        row = workflow_row(store, workflow)
        self.seed: int = row["seed"]
        self.dataset: str = row["dataset"]
        if version is None:
            self.version, self.config = active_config(store, workflow)
        else:
            self.version, self.config = version, config_at(store, workflow, version)
        self.task = label_tasks.get(row["label_task"])

    def _emit(self, event_type: type[ev._Event], **fields: Any) -> ev.Event:
        """Log a decision the workflow made by itself (no actor)."""
        event = event_type(
            at=self.at, workflow=self.workflow, config_version=self.version, **fields
        )
        return self.store.append(event)

    # --- entry points used by the engine --------------------------------------------------

    def enter(self, item: str, reason: str) -> None:
        """An item of the dataset enters the workflow and arrives at the root stages.

        ``reason`` says why now: it was in the dataset at the start, or imported later.
        """
        because = f"{self.workflow}.dataset = {self.dataset}; {reason}"
        self._emit(ev.ItemEntered, item=item, dataset=self.dataset, because=because)
        for stage in self.config.root_stages:
            cause = f"{stage.name} has no input, so it takes every item of the dataset"
            self._arrive(stage, item, source="dataset", cause=cause)
        self._finalize_if_done(item)

    def slots_closed(self, stage: str, item: str) -> None:
        """Some of the item's slots in a stage were filled or cancelled: move it on if it can."""
        self._complete_if_ready(stage, item)
        self._finalize_if_done(item)

    # --- admission ------------------------------------------------------------------------

    def _arrive(self, stage: Stage, item: str, source: str, cause: str) -> None:
        name = stage.name
        if stage.sample >= 1:
            value, admitted = None, True
            sample_text = f"{name}.sample = 1 (every item, no draw)"
        else:
            value = draw(self.seed, name, item)
            admitted = value < stage.sample
            sign = "<" if admitted else ">="
            outcome = "admitted" if admitted else "not admitted"
            sample_text = (
                f"{name}.sample = {stage.sample:g} (draw {value:.4f} {sign} {stage.sample:g}, "
                f"{outcome})"
            )
        slots: tuple[ev.NewSlot, ...] = ()
        labels_draw, slots_text = None, ""
        if admitted:
            count, reserved, labels_draw, slots_text = self._slots_needed(stage, item)
            first = next_id(self.store, "slot")
            slots = tuple(
                ev.NewSlot(
                    slot=first + i, index=i + 1, reserved_for=reserved[i] if reserved else None
                )
                for i in range(count)
            )
        self._emit(
            ev.AdmissionDecided,
            stage=name,
            item=item,
            source=source,
            admitted=admitted,
            draw=value,
            threshold=stage.sample,
            labels_per_item=_setting_json(stage.labels_per_item),
            labels_draw=labels_draw,
            slots=slots,
            because="; ".join(part for part in (cause, sample_text, slots_text) if part),
        )
        if admitted and not slots:
            self._complete(name, item)

    def _slots_needed(
        self, stage: Stage, item: str
    ) -> tuple[int, list[str] | None, float | None, str]:
        """How many slots to open: (count, who each is reserved for, the draw, because)."""
        name, setting = stage.name, stage.labels_per_item
        if setting == "all":
            everyone = holders(self.store, stage.roles)
            busy = {r["user"] for r in labels_of(self.store, self.workflow, item)}
            busy |= {
                r["claimed_by"]
                for r in self.store.select(
                    "slots", workflow=self.workflow, item=item, status="claimed"
                )
            }
            reserved = [user for user in everyone if user not in busy]
            roles = and_list(stage.roles, "or")
            if not everyone:
                text = f"{name}.labels_per_item = all (nobody holds role {roles}: no slots)"
            else:
                left_out = [u for u in everyone if u in busy]
                text = (
                    f"{name}.labels_per_item = all ({len(reserved)} slot(s), one each for "
                    f"{and_list(reserved) or 'nobody'}"
                )
                if left_out:
                    text += f"; {and_list(left_out)} already labeled or claimed this item"
                text += ")"
            return len(reserved), reserved, None, text
        if isinstance(setting, LabelsFraction):
            value = draw(self.seed, name, item, "labels_per_item")
            count = setting.count if value < setting.fraction else 1
            sign = "<" if value < setting.fraction else ">="
            text = (
                f"{name}.labels_per_item = {{count: {setting.count}, fraction: "
                f"{setting.fraction:g}}} (draw {value:.4f} {sign} {setting.fraction:g}: "
                f"{count} slot(s))"
            )
            return count, None, value, text
        return setting, None, None, f"{name}.labels_per_item = {setting}"

    # --- leaving a stage ------------------------------------------------------------------

    def _complete_if_ready(self, stage: str, item: str) -> None:
        admission = self.store.get("admissions", workflow=self.workflow, stage=stage, item=item)
        if admission is None or not admission["admitted"] or admission["completed"]:
            return
        if live_slots(self.store, self.workflow, item, stage):
            return
        self._complete(stage, item)

    def _complete(self, stage: str, item: str) -> None:
        labels = labels_of(self.store, self.workflow, item, stage)
        ids = tuple(r["label"] for r in labels)
        outcomes: list[str] = []
        if len(labels) >= 2:
            result = self._consensus(stage, item, labels)
            verdict = "agreed" if result.agreed else "disagreed"
            self._emit(
                ev.ConsensusChecked,
                stage=stage,
                item=item,
                label_ids=ids,
                agreed=result.agreed,
                agreed_label=result.agreed_label if result.agreed else None,
                rule_version=result.rule_version,
                detail=result.detail,
                because=(
                    f"{stage} gave the item {len(labels)} labels, so label task "
                    f"{self.task.ref} checked consensus (rule {result.rule_version}): {verdict}"
                ),
            )
            outcomes = ["all", verdict]
            outputs = (stage, f"{stage}.{verdict}")
            because = (
                f"no slot of {stage} is open or claimed; {len(labels)} labels that {verdict}, "
                f"so outputs {stage} and {stage}.{verdict}"
            )
        elif len(labels) == 1:
            outcomes, outputs = ["all"], (stage,)
            because = f"no slot of {stage} is open or claimed; 1 label, so output {stage}"
        else:
            outputs = ()
            because = (
                f"no slot of {stage} is open or claimed; no labels (every slot was cancelled), "
                "so the item goes to no output"
            )
        self._emit(
            ev.StageCompleted,
            stage=stage,
            item=item,
            label_ids=ids,
            outputs=outputs,
            because=because,
        )
        targets = {s.name for outcome in outcomes for s in self.config.downstream(stage, outcome)}
        for target in self.config.stages:
            if target.name in targets:
                cause = f"{target.name}.input = {target.input}"
                self._arrive(target, item, source=target.input, cause=cause)

    def _consensus(self, stage: str, item: str, labels: list[dict]) -> ConsensusResult:
        """Ask the label task whether the item's labels in the stage agree.

        The labels go to the task exactly as stored: each was validated when it was
        submitted, and checking it again against the label task registered now would turn
        an edited ontology file into a submit that can never succeed. The task's ontology
        version must be the one the labels were made under; otherwise the labels are not
        comparable and the command is refused (OntologyChanged).
        """
        current = self.task.ontology_version
        recorded = sorted({r["ontology_version"] for r in labels})
        if recorded != [current]:
            earlier = [v for v in recorded if v != current] or recorded
            raise OntologyChanged(
                f"Item {item} cannot leave stage {stage}: its labels there were made under "
                f"ontology version {and_list(earlier)}, but label task {self.task.ref} now "
                f"has ontology version {current}, so the labels cannot be compared. The "
                "ontology changed under a running workflow: bump the label task's version "
                "and start a new workflow for the new ontology. Nothing was recorded."
            )
        values = [r["value"] for r in labels]
        return ask_task(
            self.task,
            f"checking consensus on item {item} in {stage}",
            lambda: self.task.check_consensus(values),
        )

    # --- the final step -------------------------------------------------------------------

    def _finalize_if_done(self, item: str) -> None:
        row = self.store.get("workflow_items", workflow=self.workflow, item=item)
        if row["status"] != "in_progress" or live_slots(self.store, self.workflow, item):
            return
        self._finalize(item)

    def _finalize(self, item: str) -> None:
        final = self.config.resolved_final()
        fields: dict[str, Any]
        if final is None:
            fields = {"status": "none", "rule": "final: none"}
            because = "final: none (this workflow produces no final labels)"
        else:
            fields = {"status": "none", "rule": "no stage listed in final labeled it"}
            for stage in final:
                labels = labels_of(self.store, self.workflow, item, stage)
                if labels:
                    fields = self._final_from(stage, item, labels)
                    break
            because = f"final = [{', '.join(final)}]: {fields['rule']}"
        self._emit(ev.ItemFinalized, item=item, because=because, **fields)

    def _final_from(self, stage: str, item: str, labels: list[dict]) -> dict[str, Any]:
        """The final label from the first listed stage that labeled the item."""
        ids = tuple(r["label"] for r in labels)
        head = f"{stage} is the first listed stage that labeled it"
        if len(labels) == 1:
            return {
                "status": "final",
                "label": labels[0]["value"],
                "source_stage": stage,
                "label_ids": ids,
                "rule": f"{head} (1 label, label {ids[0]})",
            }
        consensus = self.store.get("consensus", workflow=self.workflow, stage=stage, item=item)
        if consensus is None:  # a stage with 2+ labels always has its consensus answer
            raise RuntimeError(f"No consensus answer for item {item} in {stage}.")
        if consensus["agreed"]:
            return {
                "status": "final",
                "label": consensus["agreed_label"],
                "source_stage": stage,
                "label_ids": ids,
                "rule": f"{head} ({len(ids)} labels that agreed; the agreed label)",
            }
        return {
            "status": "unresolved",
            "label": None,
            "source_stage": stage,
            "label_ids": ids,
            "rule": f"{head} ({len(ids)} labels that disagreed: unresolved)",
        }

    # --- scoring --------------------------------------------------------------------------

    def score(self, stage: Stage, item: str, label_id: int, user: str, label: dict) -> None:
        """Score a new label against the item's key (``score_against``). Changes no routing.

        ``label`` is the new label in the task's normal form (validated at the submit). A
        key the task rejects, however it rejects it, is logged as ``invalid_key``: scoring
        never blocks a submit.
        """
        prop = stage.score_against
        properties = self.store.get("items", dataset=self.dataset, item=item)["properties"]
        line = f"{stage.name}.score_against = {prop}"
        fields: dict[str, Any] = {"key_status": "no_key"}
        because = f"{line} (no key: item has no property {prop})"
        if prop in properties:
            key = properties[prop]
            try:
                key = self.task.validate(key)
            except Exception as error:
                fields = {"key_status": "invalid_key", "detail": {"problem": str(error)}}
                because = f"{line} (the key is not a valid label: {error})"
            else:
                result = ask_task(
                    self.task,
                    f"scoring label {label_id} against the key of item {item}",
                    lambda: self.task.score(label, key),
                )
                fields = {
                    "key_status": "scored",
                    "agreed": result.agreed,
                    "rule_version": result.rule_version,
                    "detail": result.detail,
                }
                verdict = "agreed with" if result.agreed else "did not agree with"
                because = f"{line} (label {label_id} {verdict} the key; rule {result.rule_version})"
        self._emit(
            ev.LabelScored,
            stage=stage.name,
            item=item,
            label_id=label_id,
            user=user,
            key_property=prop,
            because=because,
            **fields,
        )


def _setting_json(setting: Any) -> Any:
    """The ``labels_per_item`` setting as JSON: a number, ``all`` or ``{count, fraction}``."""
    return setting.model_dump() if isinstance(setting, LabelsFraction) else setting
