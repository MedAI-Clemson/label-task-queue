"""Forward-only edits to a running workflow: what changes, whether it applies, what it touches.

An edit is a whole new config document. ``prepare_edit`` compares it with the version in
force and classifies every change:

- **Applies in place:** a stage's ``sample``, ``labels_per_item``, ``show_prior``,
  ``score_against`` or ``assign_to``; ``final``; adding a stage; removing a stage that has
  no open or claimed slots.
- **Refused:** changing ``workflow``, ``dataset``, ``label_task`` or a stage's ``input``.
  These change what existing labels mean, so they need a new workflow. Also refused:
  re-using the name of a stage removed earlier (its admissions, draws and labels are
  recorded under that name), changing the order of existing stages (it decides the default
  work order), and removing a stage that still has open or claimed slots (cancel them first).

A document diff cannot tell a rename from a removal plus an addition, so a rename is
treated as both, with a warning when the two stages have the same input and roles.

**Forward-only.** A new version governs only decisions made after it is activated, and
nothing is recomputed at activation: admissions, slots, labels and final labels keep the
version they were made under, open slots stay open, and items already past a stage are not
sent back to it (backfill is deferred). The engine makes every decision with the version in
force at that moment, so this needs no special machinery.

**Pauses** are not part of the config. A paused stage stays paused in every later version
while it exists, and removing a paused stage is allowed on the usual terms (no open or
claimed slots). The preview mentions each pause in force so the admin is not surprised.

**The counts** describe the items already in the workflow at the moment of the preview (see
``StageImpact`` and ``FinalImpact``). Items imported later always follow the version in force
when they arrive.
"""

from __future__ import annotations

import difflib
import textwrap
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from labelflow.engine.config import (
    ValidationContext,
    WorkflowConfig,
    resolve,
    resolved_yaml,
    validate_config,
)
from labelflow.engine.config.problems import percent, plural
from labelflow.engine.draws import draw
from labelflow.engine.queries import (
    active_config,
    active_pause,
    and_list,
    config_at,
    held_roles,
    pause_text,
    role_holders,
    workflow_row,
)
from labelflow.engine.tables import Row, Tables

ConfigSource = WorkflowConfig | Mapping[str, Any] | str
"""A proposed config: a parsed config, a mapping, or YAML text."""

STAGE_KEYS = ("input", "assign_to", "sample", "labels_per_item", "score_against", "show_prior")
"""The stage keys compared between versions (``name`` identifies the stage)."""


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class Change(_View):
    """One difference between the active config and the proposed one."""

    kind: str
    """``workflow``, ``dataset``, ``label_task``, ``final``, ``order``, ``stage_added``,
    ``stage_removed``, or the stage key that changed (one of ``STAGE_KEYS``)."""
    stage: str | None = None
    before: JsonValue = None
    after: JsonValue = None
    applies: Literal["in_place", "refused"]
    summary: str
    """What changed, in config terms, e.g. ``expert_review.sample: 0.2 -> 0.3``."""
    effect: str
    """When it takes effect; for a refused change, why, and what to do instead."""


class ItemCounts(_View):
    """The items that entered the workflow, by status, at the moment of the preview."""

    entered: int
    in_progress: int
    finished: int
    """Items with their final-label record (status final, unresolved or none)."""
    excluded: int


class StageImpact(_View):
    """What an edit touches in one stage, counted over the items already in the workflow.

    Every item that entered the workflow is counted in exactly one of ``in_stage``,
    ``may_arrive``, ``past`` and ``cannot_arrive``. A changed or added stage is counted
    under the proposed config; a removed stage under the active one.
    """

    stage: str
    status: Literal["changed", "added", "removed"]
    open_slots: int
    """Open slots of the stage now. They stay open; a new ``assign_to``, ``show_prior`` or
    ``score_against`` reaches them at the next claim."""
    claimed_slots: int
    """Claimed slots of the stage now. They stay with their claimant."""
    in_stage: int
    """Items in progress that were admitted to the stage and have not left it. They keep the
    slots they were given."""
    may_arrive: int
    """Items in progress that have not arrived at the stage yet and still can, given what has
    happened so far: they are in, or may still reach, the stage it takes items from, and for
    an ``.agreed``/``.disagreed`` input they have two or more slots there. Their arrival is
    decided under the new version. It is an upper bound: whether each one arrives depends
    on decisions not made yet (upstream sampling, consensus, cancelled slots)."""
    past: int
    """Items that arrived at the stage earlier and are no longer in it: not admitted,
    admitted and left, or excluded. They keep that decision (no backfill)."""
    cannot_arrive: int
    """Items that never arrived at the stage and never will: finished or excluded, or past
    the point where they could have reached it. A stage without ``input`` is reached only
    when an item enters the workflow, so an added one counts every item already in it."""
    admitted_at_old_sample: int | None = None
    """For a ``sample`` change: how many of the ``may_arrive`` items have a draw below the
    old sample. Draws are hashed from (seed, stage, item), so they are known before the item
    arrives: these are exactly the items each sample would admit if they arrive."""
    admitted_at_new_sample: int | None = None
    """For a ``sample`` change: how many of the ``may_arrive`` items have a draw below the
    new sample."""


class FinalImpact(_View):
    """What a change to ``final`` touches."""

    not_finalized: int
    """Items in progress: they will be finalized under the new final step."""
    finalized: int
    """Items that already have their final-label record: unaffected."""
    excluded: int
    """Excluded items: they get no final label under any version."""


class ChangePreview(_View):
    """What activating a proposed config would change, before it is activated.

    ``changes`` lists every difference, classified. ``errors`` says why the edit cannot be
    activated (empty when it can) and ``warnings`` what to double-check. ``diff`` is a
    unified diff of the two resolved YAML documents. ``items``, ``stages`` and ``final``
    count what the edit touches.
    """

    workflow: str
    base_version: int
    """The active version, which the proposal is compared with."""
    new_version: int
    """The number the proposal gets if it is activated."""
    changes: tuple[Change, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    diff: str
    items: ItemCounts
    stages: tuple[StageImpact, ...]
    final: FinalImpact | None
    """Set when ``final`` changes (spelled out, so adding or removing a stage changes it
    when the config leaves ``final`` out)."""

    @property
    def ok(self) -> bool:
        """Whether the edit can be activated."""
        return not self.errors

    def counts(self) -> dict[str, Any]:
        """The counts as plain JSON, as logged with the activation."""
        return {
            "items": self.items.model_dump(),
            "stages": [impact.model_dump() for impact in self.stages],
            "final": self.final.model_dump() if self.final else None,
        }

    def render(self) -> str:
        """The preview as plain text, for an admin."""
        lines = [
            f"Change preview for workflow '{self.workflow}': version {self.base_version} "
            f"(active) -> version {self.new_version} (proposed)",
            "",
        ]
        if self.ok:
            lines.append(
                f"This edit can be activated. It makes {plural(len(self.changes), 'change')}, "
                "all applying in place."
            )
        else:
            lines.append(
                f"This edit cannot be activated: {plural(len(self.errors), 'problem')} "
                "(see Refused)."
            )
        if self.changes:
            lines += ["", "Changes:"]
            for number, change in enumerate(self.changes, start=1):
                tag = "applies in place" if change.applies == "in_place" else "refused"
                lines.append(f"  {number}. {change.summary}  [{tag}]")
                lines.append(_wrap(change.effect, "     "))
        for title, notes in (("Refused", self.errors), ("Warnings", self.warnings)):
            if notes:
                lines += ["", f"{title}:"]
                lines += [_wrap(note, "    ", "  - ") for note in notes]
        if self.stages or self.final:
            lines += ["", *self._touches()]
        if self.diff:
            lines += ["", "Diff of the resolved configs:", self.diff.rstrip("\n")]
        return "\n".join(lines) + "\n"

    def _touches(self) -> list[str]:
        items = self.items
        lines = [
            _wrap(
                f"What it touches. The workflow has {plural(items.entered, 'item')}: "
                f"{items.in_progress} in progress, {items.finished} finished, {items.excluded} "
                "excluded. Items imported later always follow the version in force when they "
                "arrive.",
                "",
            )
        ]
        for impact in self.stages:
            kinds = {c.kind for c in self.changes if c.stage == impact.stage}
            sample = next(
                (c for c in self.changes if c.stage == impact.stage and c.kind == "sample"), None
            )
            lines += ["", f"  Stage '{impact.stage}' ({impact.status}):"]
            lines += [
                _wrap(text, "      ", "    - ")
                for text in _impact_notes(impact, kinds, sample, self.new_version)
            ]
        if self.final is not None:
            final = self.final
            lines += ["", "  Final step (changed):"]
            notes = [
                f"Not finalized yet: {_count(final.not_finalized)}. Their final labels will "
                f"follow version {self.new_version}.",
                f"Unaffected: {_count(final.finalized)} already finalized (they keep their final "
                f"labels) and {_count(final.excluded)} excluded (no final label).",
            ]
            lines += [_wrap(text, "      ", "    - ") for text in notes]
        return lines


def _impact_notes(
    impact: StageImpact, kinds: set[str], sample: Change | None, new_version: int
) -> list[str]:
    """The plain-language lines for one stage's counts (see ``StageImpact``)."""
    slots = f"Slots now: {impact.open_slots} open and {impact.claimed_slots} claimed"
    if impact.status == "removed":
        return [
            f"{slots} (removing the stage needs none).",
            f"Could still have arrived: {_count(impact.may_arrive)} in progress. After "
            "activation none will.",
            f"Arrived earlier: {_count(impact.past + impact.in_stage)}. Their labels and "
            "decisions stay in the log, in explain and in the exports.",
        ]
    notes = []
    if impact.status == "changed":
        if "assign_to" in kinds:
            notes.append(
                f"{slots}. The new assign_to reaches the open ones at the next claim; claimed "
                "slots stay with their claimant."
            )
        else:
            notes.append(f"{slots}. They stay as they are.")
        if impact.in_stage:
            notes.append(
                f"In the stage now: {_count(impact.in_stage)}, with the slots already opened."
            )
    notes.append(
        f"May still arrive: {_count(impact.may_arrive)} in progress, decided under version "
        f"{new_version} if they do."
    )
    if sample is not None and impact.admitted_at_old_sample is not None:
        notes.append(
            "Their draws are fixed in advance (hashed per stage and item), so it is known "
            f"which would be admitted if they arrive: {impact.admitted_at_old_sample} at the "
            f"old sample {sample.before:g}, {impact.admitted_at_new_sample} at the new "
            f"{sample.after:g}."
        )
    unaffected = f"{_count(impact.cannot_arrive)} that cannot arrive any more"
    if impact.status == "changed":
        unaffected = f"{_count(impact.past)} already decided here, and {unaffected}"
    notes.append(f"Unaffected (no backfill): {unaffected}.")
    return notes


def _count(n: int) -> str:
    return plural(n, "item")


def _wrap(text: str, indent: str, first: str | None = None) -> str:
    return textwrap.fill(
        text,
        width=92,
        initial_indent=indent if first is None else first,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


# --- classifying changes -------------------------------------------------------------------


def classify(
    old: WorkflowConfig,
    new: WorkflowConfig,
    removed_before: Mapping[str, int],
    open_slots: Mapping[str, int],
    claimed_slots: Mapping[str, int],
) -> tuple[list[Change], list[str]]:
    """Every change from ``old`` to ``new``, classified, plus warnings.

    Both configs are compared as resolved documents (every default filled in), so writing a
    default out or leaving it implicit is not a change. ``removed_before`` maps the names of
    stages removed by earlier edits to the version that removed them; ``open_slots`` and
    ``claimed_slots`` count a stage's live slots now.
    """
    changes = _top_level_changes(old, new)
    before = {stage["name"]: stage for stage in resolve(old)["stages"]}
    after = {stage["name"]: stage for stage in resolve(new)["stages"]}
    for name, stage in after.items():
        if name not in before:
            changes.append(_stage_added(stage, removed_before))
            continue
        for key in STAGE_KEYS:
            if stage[key] != before[name][key]:
                changes.append(_stage_key_changed(name, key, before[name][key], stage[key]))
    for name, stage in before.items():
        if name not in after:
            changes.append(
                _stage_removed(stage, open_slots.get(name, 0), claimed_slots.get(name, 0))
            )
    order = _order_change(old, new)
    if order is not None:
        changes.append(order)
    old_final, new_final = resolve(old)["final"], resolve(new)["final"]
    if old_final != new_final:
        changes.append(
            Change(
                kind="final",
                before=old_final,
                after=new_final,
                applies="in_place",
                summary=f"final: {_show(old_final)} -> {_show(new_final)}",
                effect=_final_effect(new_final),
            )
        )
    removed = [before[name] for name in before if name not in after]
    added = [after[name] for name in after if name not in before]
    return changes, _rename_warnings(removed, added)


def _top_level_changes(old: WorkflowConfig, new: WorkflowConfig) -> list[Change]:
    """Changes to ``workflow``, ``dataset`` and ``label_task``: each needs a new workflow."""
    reasons = {
        "workflow": (
            f"The config names workflow '{new.workflow}', but this edit is to workflow "
            f"'{old.workflow}'. A workflow keeps its name: to run under the name "
            f"'{new.workflow}', start a new workflow."
        ),
        "dataset": (
            f"Changing the dataset from '{old.dataset}' to '{new.dataset}' needs a new "
            f"workflow: this workflow's items and labels belong to '{old.dataset}'. Start a "
            f"new workflow over '{new.dataset}'."
        ),
        "label_task": (
            f"Changing the label task from '{old.label_task}' to '{new.label_task}' needs a "
            "new workflow: it changes what a label is, so earlier labels could not be compared "
            f"with new ones. Start a new workflow with '{new.label_task}'."
        ),
    }
    changes = []
    for key, reason in reasons.items():
        before, after = getattr(old, key), getattr(new, key)
        if before != after:
            changes.append(
                Change(
                    kind=key,
                    before=before,
                    after=after,
                    applies="refused",
                    summary=f"{key}: {before} -> {after}",
                    effect=reason,
                )
            )
    return changes


def _stage_key_changed(name: str, key: str, before: Any, after: Any) -> Change:
    """A change to one key of a stage that is in both versions."""
    applies: Literal["in_place", "refused"] = "in_place"
    if key == "input":
        applies = "refused"
        effect = (
            f"Changing the input of stage '{name}' from {_source(before)} to {_source(after)} "
            f"needs a new workflow: the items already in '{name}' came from {_source(before)}, "
            "so its labels would mean something different. Start a new workflow, or add a new "
            f"stage that takes {_source(after)}."
        )
    elif key == "sample":
        effect = (
            f"Items that arrive at '{name}' after activation are admitted when their draw is "
            f"below {after:g}; items that already arrived keep their decision (no backfill)."
        )
    elif key == "labels_per_item":
        effect = (
            f"Items admitted to '{name}' after activation get {_labels_text(after)}; items "
            "already admitted keep the slots they have."
        )
    elif key == "assign_to":
        effect = (
            f"Applies at the next claim, open slots included: the slots of '{name}' can then be "
            f"claimed by holders of {and_list(after, 'or')}. Claimed slots stay with their "
            "claimant."
        )
    elif key == "show_prior":
        shown = "shown earlier labels" if after else "not shown earlier labels (blind)"
        effect = (
            f"From activation on, labelers in '{name}' are {shown}, open and claimed slots "
            "included. Each label records what was shown, so nothing submitted changes."
        )
    elif after is None:
        effect = (
            f"Labels submitted in '{name}' after activation are not scored; earlier labels keep "
            "their scores."
        )
    else:
        effect = (
            f"Labels submitted in '{name}' after activation, from open and claimed slots too, "
            f"are scored against the item property '{after}'; earlier labels keep their scores."
        )
    shown_before, shown_after = (
        (_source_short(before), _source_short(after))
        if key == "input"
        else (_show(before), _show(after))
    )
    return Change(
        kind=key,
        stage=name,
        before=before,
        after=after,
        applies=applies,
        summary=f"{name}.{key}: {shown_before} -> {shown_after}",
        effect=effect,
    )


def _stage_added(stage: dict[str, Any], removed_before: Mapping[str, int]) -> Change:
    name, source = stage["name"], stage["input"]
    applies: Literal["in_place", "refused"] = "in_place"
    if name in removed_before:
        applies = "refused"
        effect = (
            f"Stage '{name}' cannot be added: a stage of that name was removed in version "
            f"{removed_before[name]}, and its admissions, draws and labels are still recorded "
            "under that name. Give the new stage another name."
        )
    elif source is None:
        effect = (
            f"Items imported after activation arrive at '{name}'; items already in the "
            "workflow do not (no backfill)."
        )
    else:
        upstream, _, outcome = source.partition(".")
        only = f" if their labels {outcome}" if outcome else ""
        effect = (
            f"Items that leave '{upstream}' after activation arrive at '{name}'{only}; items "
            f"that already left '{upstream}' do not (no backfill)."
        )
    return Change(
        kind="stage_added",
        stage=name,
        after=stage,
        applies=applies,
        summary=f"stage {name} added (input: {_source_short(source)})",
        effect=effect,
    )


def _stage_removed(stage: dict[str, Any], open_slots: int, claimed_slots: int) -> Change:
    name = stage["name"]
    if open_slots or claimed_slots:
        live = [
            plural(n, f"{state} slot")
            for n, state in ((open_slots, "open"), (claimed_slots, "claimed"))
            if n
        ]
        applies: Literal["in_place", "refused"] = "refused"
        effect = (
            f"Stage '{name}' still has {and_list(live)}, so it cannot be removed yet. Cancel "
            "its open slots first (a claimed slot must be submitted or released, and then "
            "cancelled), then preview the edit again."
        )
    else:
        applies = "in_place"
        effect = (
            f"'{name}' receives no new items and leaves the work order; its labels and "
            "decisions stay in the log, in explain and in the exports."
        )
    return Change(
        kind="stage_removed",
        stage=name,
        before=stage,
        applies=applies,
        summary=f"stage {name} removed",
        effect=effect,
    )


def _order_change(old: WorkflowConfig, new: WorkflowConfig) -> Change | None:
    """A change in the relative order of the stages in both versions, or None."""
    kept_before = [name for name in old.stage_names if name in new.stage_names]
    kept_after = [name for name in new.stage_names if name in old.stage_names]
    if kept_before == kept_after:
        return None
    return Change(
        kind="order",
        before=kept_before,
        after=kept_after,
        applies="refused",
        summary=f"order of stages: {', '.join(kept_before)} -> {', '.join(kept_after)}",
        effect=(
            f"The existing stages are listed in a different order ({', '.join(kept_before)} "
            f"becomes {', '.join(kept_after)}). Their order decides the default work order "
            "(later-listed stages are offered first), so it cannot change in a running "
            "workflow. Keep the existing stages in order; new stages can go anywhere their "
            "inputs allow."
        ),
    )


def _rename_warnings(removed: list[dict[str, Any]], added: list[dict[str, Any]]) -> list[str]:
    """Warn when a removed and an added stage look like one stage renamed."""
    warnings = []
    for gone in removed:
        for new in added:
            if gone["input"] == new["input"] and sorted(gone["assign_to"]) == sorted(
                new["assign_to"]
            ):
                warnings.append(
                    f"Stage '{gone['name']}' is removed and stage '{new['name']}' is added with "
                    f"the same input ({_source_short(new['input'])}) and roles "
                    f"({and_list(new['assign_to'])}). If this is meant as a rename: renaming a "
                    "stage needs a new workflow, because labels and decisions are recorded "
                    f"under the stage's name. As written, '{gone['name']}' stops receiving "
                    f"items and '{new['name']}' starts empty (no backfill)."
                )
    return warnings


def _final_effect(final: list[str] | str) -> str:
    if final == "none":
        return (
            "Items finalized after activation get no final label (final: none); items already "
            "finalized keep theirs."
        )
    return (
        "Items finalized after activation take their final label from the first of "
        f"{', '.join(final)} that labeled them; items already finalized keep theirs."
    )


def _labels_text(value: Any) -> str:
    if value == "all":
        return "one slot for each person holding its roles at that moment"
    if isinstance(value, dict):
        return (
            f"{value['count']} labels for {percent(value['fraction'])} of them and 1 label for "
            "the rest"
        )
    return plural(value, "label") + " each"


def _source(value: str | None) -> str:
    return "every item of the dataset" if value is None else f"'{value}'"


def _source_short(value: str | None) -> str:
    return "the dataset" if value is None else value


def _show(value: Any) -> str:
    """A resolved config value, written as in the config."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return "[" + ", ".join(_show(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_show(v)}" for k, v in value.items()) + "}"
    return str(value)


# --- counting what an edit touches ---------------------------------------------------------


@dataclass(frozen=True)
class _Progress:
    """The workflow's items, arrivals and live slots, read once for a preview."""

    seed: int
    status: Mapping[str, str]
    """Item -> in_progress, done or excluded."""
    arrivals: Mapping[tuple[str, str], Row]
    """(stage, item) -> the admission row: every arrival of an item at a stage."""
    open: Counter[str]
    claimed: Counter[str]

    @classmethod
    def read(cls, tables: Tables, workflow: str) -> _Progress:
        def slots(status: str) -> Counter[str]:
            rows = tables.select("slots", workflow=workflow, status=status)
            return Counter(row["stage"] for row in rows)

        return cls(
            seed=workflow_row(tables, workflow)["seed"],
            status={
                r["item"]: r["status"] for r in tables.select("workflow_items", workflow=workflow)
            },
            arrivals={
                (r["stage"], r["item"]): r for r in tables.select("admissions", workflow=workflow)
            },
            open=slots("open"),
            claimed=slots("claimed"),
        )

    def item_counts(self) -> ItemCounts:
        counts = Counter(self.status.values())
        return ItemCounts(
            entered=len(self.status),
            in_progress=counts["in_progress"],
            finished=counts["done"],
            excluded=counts["excluded"],
        )

    def final_impact(self) -> FinalImpact:
        counts = Counter(self.status.values())
        return FinalImpact(
            not_finalized=counts["in_progress"],
            finalized=counts["done"],
            excluded=counts["excluded"],
        )

    def may_arrive(self, config: WorkflowConfig, stage: str, item: str) -> bool:
        """Whether an item that has not arrived at the stage yet still can (see
        ``StageImpact.may_arrive``)."""
        if (stage, item) in self.arrivals or self.status[item] != "in_progress":
            return False
        ref = config.stage(stage).input_ref
        if ref is None:
            return False  # stages without input are reached only on entering the workflow
        upstream = self.arrivals.get((ref.stage, item))
        if upstream is None:
            return self.may_arrive(config, ref.stage, item)
        if not upstream["admitted"] or upstream["completed"]:
            return False  # not admitted upstream, or it already left: no second arrival
        return ref.outcome == "all" or upstream["n_slots"] >= 2  # 1 label never (dis)agrees

    def impact(
        self,
        config: WorkflowConfig,
        stage: str,
        status: Literal["changed", "added", "removed"],
        old_sample: float | None = None,
    ) -> StageImpact:
        """Count the items in each of the four groups of ``StageImpact`` for one stage."""
        groups: Counter[str] = Counter()
        arriving = []
        for item, state in self.status.items():
            arrival = self.arrivals.get((stage, item))
            if arrival is not None:
                inside = state == "in_progress" and arrival["admitted"] and not arrival["completed"]
                groups["in_stage" if inside else "past"] += 1
            elif self.may_arrive(config, stage, item):
                arriving.append(item)
            else:
                groups["cannot_arrive"] += 1
        sample_counts = {}
        if old_sample is not None:
            new_sample = config.stage(stage).sample
            draws = [draw(self.seed, stage, item) for item in arriving]
            sample_counts = {
                "admitted_at_old_sample": sum(value < old_sample for value in draws),
                "admitted_at_new_sample": sum(value < new_sample for value in draws),
            }
        return StageImpact(
            stage=stage,
            status=status,
            open_slots=self.open[stage],
            claimed_slots=self.claimed[stage],
            in_stage=groups["in_stage"],
            may_arrive=len(arriving),
            past=groups["past"],
            cannot_arrive=groups["cannot_arrive"],
            **sample_counts,
        )


def _impacts(
    old: WorkflowConfig, new: WorkflowConfig, changes: list[Change], progress: _Progress
) -> list[StageImpact]:
    """Counts for every stage a change touches, in the proposed order, then removed stages."""
    kinds: dict[str, set[str]] = {}
    for change in changes:
        if change.stage is not None:
            kinds.setdefault(change.stage, set()).add(change.kind)
    impacts = []
    for stage in new.stages:
        touched = kinds.get(stage.name, set())
        if "stage_added" in touched:
            impacts.append(progress.impact(new, stage.name, "added"))
        elif touched:
            old_sample = old.stage(stage.name).sample if "sample" in touched else None
            impacts.append(progress.impact(new, stage.name, "changed", old_sample))
    for stage in old.stages:
        if stage.name not in new.stage_names:
            impacts.append(progress.impact(old, stage.name, "removed"))
    return impacts


# --- the preview ----------------------------------------------------------------------------


def _removed_earlier(tables: Tables, workflow: str, active: int) -> dict[str, int]:
    """Stages that earlier edits removed: name -> the version that removed it."""
    current = set(config_at(tables, workflow, active).stage_names)
    last_seen: dict[str, int] = {}
    for version in range(1, active):
        for name in config_at(tables, workflow, version).stage_names:
            last_seen[name] = version
    return {name: version + 1 for name, version in last_seen.items() if name not in current}


def _stranded_slot_warnings(tables: Tables, workflow: str, changes: list[Change]) -> list[str]:
    """Warn when a new ``assign_to`` leaves open slots reserved for people without its roles.

    ``labels_per_item: all`` reserves each slot for one person. If that person holds none of
    the stage's new roles, the slot waits until they get one or the slot is cancelled.
    """
    warnings = []
    for change in changes:
        if change.kind != "assign_to" or not isinstance(change.after, list):
            continue
        roles = {str(role) for role in change.after}
        open_slots = tables.select("slots", workflow=workflow, stage=change.stage, status="open")
        stranded = [
            slot
            for slot in open_slots
            if slot["reserved_for"] and not held_roles(tables, slot["reserved_for"]) & roles
        ]
        if stranded:
            people = sorted({slot["reserved_for"] for slot in stranded})
            warnings.append(
                f"{plural(len(stranded), 'open slot')} of '{change.stage}' "
                f"{'is' if len(stranded) == 1 else 'are'} reserved for {and_list(people)} "
                f"(labels_per_item: all), who {'holds' if len(people) == 1 else 'hold'} none "
                f"of its new roles ({and_list(sorted(roles))}). Such a slot waits until its "
                "person is given one of the roles, or until it is cancelled."
            )
    return warnings


def _pause_warnings(tables: Tables, workflow: str, new: WorkflowConfig, base: int) -> list[str]:
    """Say what happens to each pause in force: pauses live outside the config, so they carry
    over to the new version, or end with a removed stage."""
    warnings = []
    paused = active_pause(tables, workflow)
    if paused is not None:
        warnings.append(
            f"The workflow is paused ({pause_text(paused)}); it stays paused under version "
            f"{base + 1}."
        )
    for name in config_at(tables, workflow, base).stage_names:
        paused = active_pause(tables, workflow, name)
        if paused is None:
            continue
        if name in new.stage_names:
            warnings.append(
                f"Stage '{name}' is paused ({pause_text(paused)}); it stays paused under "
                f"version {base + 1}."
            )
        else:
            warnings.append(f"Stage '{name}' is paused; removing it ends the pause with it.")
    return warnings


def _diff(old: WorkflowConfig, new: WorkflowConfig, base: int) -> str:
    lines = difflib.unified_diff(
        resolved_yaml(old).splitlines(),
        resolved_yaml(new).splitlines(),
        fromfile=f"version {base} (active)",
        tofile=f"version {base + 1} (proposed)",
        lineterm="",
    )
    return "".join(f"{line}\n" for line in lines)


def prepare_edit(
    tables: Tables, workflow: str, source: ConfigSource
) -> tuple[ChangePreview, WorkflowConfig | None]:
    """Compare a proposed config with the active version, reading only.

    Returns the preview and the parsed proposal (None when it is not a valid config). The
    proposal is checked like a new config (graph rules, coverage, roles that exist), then
    every change is classified and counted.
    """
    base, old = active_config(tables, workflow)
    progress = _Progress.read(tables, workflow)
    if isinstance(source, WorkflowConfig):
        source = source.model_dump(mode="json")
    report = validate_config(source, ValidationContext(roles=role_holders(tables)))
    errors = [str(problem) for problem in report.errors]
    warnings = [str(problem) for problem in report.warnings]
    new = report.config
    changes: list[Change] = []
    impacts: list[StageImpact] = []
    final, diff = None, ""
    if new is not None:
        changes, more_warnings = classify(
            old,
            new,
            _removed_earlier(tables, workflow, base),
            progress.open,
            progress.claimed,
        )
        warnings += more_warnings + _stranded_slot_warnings(tables, workflow, changes)
        warnings += _pause_warnings(tables, workflow, new, base)
        errors += [change.effect for change in changes if change.applies == "refused"]
        if not changes:
            errors.append(
                f"The proposed config is the same as version {base} once defaults are filled "
                "in, so there is nothing to activate."
            )
        impacts = _impacts(old, new, changes, progress)
        if any(change.kind == "final" for change in changes):
            final = progress.final_impact()
        diff = _diff(old, new, base)
    preview = ChangePreview(
        workflow=workflow,
        base_version=base,
        new_version=base + 1,
        changes=tuple(changes),
        errors=tuple(errors),
        warnings=tuple(warnings),
        diff=diff,
        items=progress.item_counts(),
        stages=tuple(impacts),
        final=final,
    )
    return preview, new
