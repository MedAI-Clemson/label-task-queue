"""What the interface reports having shown a labeler, checked against the log.

A label carries ``Shown``: the content hash of the image, the ids of the earlier labels
shown, whether earlier labels were offered, the consensus rule behind any flags, and the
earlier label the labeler started from, if they copied one into the editor. Each is checked
against what the work packet could have given, so a label never claims a view it could not
have had.
"""

from __future__ import annotations

from labelflow.engine import events as ev
from labelflow.engine.config import and_list
from labelflow.engine.errors import InvalidShown
from labelflow.engine.events import Shown
from labelflow.engine.queries import config_at, item_row, labels_of
from labelflow.engine.store import Store
from labelflow.engine.tables import Row


def check_shown(store: Store, slot: Row, version: int, shown: Shown) -> None:
    """Refuse a report that does not fit the item, its labels or the stage (InvalidShown).

    ``show_prior`` must be what the stage showed when the slot was claimed (or under a
    version activated since: an edit that makes a stage blind, or the reverse, must not
    reject the label of someone who claimed before it). Earlier labels must be labels of
    this item from other stages of the workflow, which is all the work packet ever shows.
    A label can start from an earlier label only if that label is among those reported
    shown, so only in a stage that showed earlier labels (under the same version rule).
    """
    workflow, item, stage = slot["workflow"], slot["item"], slot["stage"]
    content_hash = item_row(store, workflow, item)["content_hash"]
    if shown.content_hash != content_hash:
        raise InvalidShown(
            f"The interface reports showing content {shown.content_hash}, but item {item} "
            f"has content hash {content_hash}."
        )
    offered = _show_prior_since_claim(store, slot, version)
    if shown.show_prior not in offered:
        if shown.show_prior:
            raise InvalidShown(
                f"Stage {stage} is blind (show_prior: false), so no earlier labels were "
                "offered for this slot, but the interface reports show_prior: true."
            )
        raise InvalidShown(
            f"Stage {stage} offers earlier labels (show_prior: true), but the interface "
            "reports show_prior: false. Report exactly what the work packet gave."
        )
    if shown.prior_label_ids:
        if not shown.show_prior:
            raise InvalidShown("Earlier labels are reported as shown, but show_prior is false.")
        known = {r["label"]: r for r in labels_of(store, workflow, item)}
        if strangers := [i for i in shown.prior_label_ids if i not in known]:
            raise InvalidShown(
                f"Label(s) {and_list(strangers)} are not labels of item {item} in workflow "
                f"{workflow}."
            )
        if same := [i for i in shown.prior_label_ids if known[i]["stage"] == stage]:
            raise InvalidShown(
                f"Label(s) {and_list(same)} are labels of {stage} itself, the stage being "
                f"labeled; the work packet shows only labels of item {item} from other "
                "stages, so they cannot have been shown."
            )
    if shown.started_from is not None:
        _check_started_from(stage, shown)
    if shown.consensus_rule_version is not None:
        rules = {r["rule_version"] for r in store.select("consensus", workflow=workflow, item=item)}
        if shown.consensus_rule_version not in rules:
            raise InvalidShown(
                f"No consensus check on item {item} used rule {shown.consensus_rule_version}."
            )


def _check_started_from(stage: str, shown: Shown) -> None:
    """``started_from`` must name one of the earlier labels reported as shown."""
    label = shown.started_from
    if not shown.show_prior:
        raise InvalidShown(
            f"The label reports starting from label {label}, but no earlier labels were "
            f"shown (show_prior: false): stage {stage} is blind, so there was no earlier "
            "label to start from."
        )
    if label not in shown.prior_label_ids:
        offered = and_list(str(i) for i in shown.prior_label_ids) or "none"
        raise InvalidShown(
            f"The label reports starting from label {label}, but label {label} is not among "
            f"the earlier labels reported as shown (shown: {offered}). A label can start "
            "only from an earlier label that was shown."
        )


def _show_prior_since_claim(store: Store, slot: Row, version: int) -> set[bool]:
    """The ``show_prior`` settings the stage has had from the version in force at the slot's
    (latest) claim to the version in force now.

    Usually one value. Two after an edit that made the stage blind, or the reverse, since
    the claim: the work packet may have been fetched on either side of the edit, so a label
    reporting either is accepted.
    """
    workflow, stage = slot["workflow"], slot["stage"]
    claims = [
        e
        for e in store.events(workflow=workflow, item=slot["item"], type="slot_claimed")
        if isinstance(e, ev.SlotClaimed) and e.slot == slot["slot"]
    ]
    since = claims[-1].config_version if claims else version
    settings = set()
    for earlier in range(since, version + 1):
        config = config_at(store, workflow, earlier)
        if stage in config.stage_names:
            settings.add(config.stage(stage).show_prior)
    return settings
