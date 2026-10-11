"""What the interface reports having shown a labeler, checked against the log.

A label carries ``Shown``: the content hash of the image, the ids of the earlier labels
shown, whether earlier labels were offered, and the consensus rule behind any flags. Each is
checked, so a label never claims a view it could not have had.
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
    """Refuse a report that does not fit the item, its labels or the stage (InvalidShown)."""
    workflow, item = slot["workflow"], slot["item"]
    content_hash = item_row(store, workflow, item)["content_hash"]
    if shown.content_hash != content_hash:
        raise InvalidShown(
            f"The interface reports showing content {shown.content_hash}, but item {item} "
            f"has content hash {content_hash}."
        )
    if shown.prior_label_ids:
        if not shown.show_prior:
            raise InvalidShown("Earlier labels are reported as shown, but show_prior is false.")
        if not _could_show_prior(store, slot, version):
            raise InvalidShown(
                f"Stage {slot['stage']} is blind (show_prior: false), but the interface "
                "reports showing earlier labels."
            )
        known = {r["label"] for r in labels_of(store, workflow, item)}
        if strangers := [i for i in shown.prior_label_ids if i not in known]:
            raise InvalidShown(
                f"Label(s) {and_list(strangers)} are not labels of item {item} in workflow "
                f"{workflow}."
            )
    if shown.consensus_rule_version is not None:
        rules = {r["rule_version"] for r in store.select("consensus", workflow=workflow, item=item)}
        if shown.consensus_rule_version not in rules:
            raise InvalidShown(
                f"No consensus check on item {item} used rule {shown.consensus_rule_version}."
            )


def _could_show_prior(store: Store, slot: Row, version: int) -> bool:
    """Whether the stage showed earlier labels under the version in force now, or under any
    version since the slot was claimed: an edit that makes a stage blind must not reject a
    label whose author was legitimately shown earlier labels before it."""
    workflow, stage = slot["workflow"], slot["stage"]
    claims = [
        e
        for e in store.events(workflow=workflow, item=slot["item"], type="slot_claimed")
        if isinstance(e, ev.SlotClaimed) and e.slot == slot["slot"]
    ]
    since = claims[-1].config_version if claims else version
    for earlier in range(since, version + 1):
        config = config_at(store, workflow, earlier)
        if stage in config.stage_names and config.stage(stage).show_prior:
            return True
    return False
