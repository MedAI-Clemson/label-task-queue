"""The two export tables and the reports built from the derived tables.

Every row is a flat dict of plain values, ready for CSV or Parquet: composite values (a
label, a list of label ids, a list of roles) are given as canonical JSON text.

- ``export_items``: one row per item, with its final label and how it was decided.
- ``export_labels``: one row per label, with who made it, the roles they held then, the
  stage, the config version, what they were shown and the score against a key where set.
- ``score_report``: per stage and labeler, how many labels agreed with the key.
- ``sampling_report``: per stage, how many items arrived and how many were admitted, also
  per upstream labeler, so the actual sampling rates can be checked.

Stages removed by an edit keep their rows: their history stays visible.

Excluded items keep their rows too. The items export marks them ``excluded`` (keeping an
earlier final label as history), while their labels, scores and arrivals stay in the labels
export and both reports: they happened, and they say how labelers did and how sampling went,
whatever later became of the item. Filter on the items export's ``status`` to leave them out.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from labelflow.engine.events import canonical_json
from labelflow.engine.queries import (
    active_config,
    export_status,
    labels_of,
    stages_ever,
    workflow_row,
)
from labelflow.engine.tables import Tables

ExportRow = dict[str, Any]


def _json(value: Any) -> str | None:
    return None if value is None else canonical_json(value)


def export_items(tables: Tables, workflow: str) -> list[ExportRow]:
    """One row per item that entered the workflow, in the order they entered.

    ``status`` is in_progress, final, unresolved, none or excluded. ``final_status`` and the
    columns after it describe the item's final-label record, if it has one. An item excluded
    after it was finalized has status ``excluded`` and keeps that record in these columns
    (``final_status`` says what it was), so only rows with status ``final`` are final labels
    to use.
    """
    dataset = workflow_row(tables, workflow)["dataset"]
    rows = []
    entered = sorted(
        tables.select("workflow_items", workflow=workflow), key=lambda r: r["entered_seq"]
    )
    for entry in entered:
        item = tables.get("items", dataset=dataset, item=entry["item"])
        final = tables.get("final_labels", workflow=workflow, item=entry["item"]) or {}
        rows.append(
            {
                "workflow": workflow,
                "item": entry["item"],
                "uri": item["uri"],
                "content_hash": item["content_hash"],
                "status": export_status(tables, workflow, entry["item"]),
                "final_status": final.get("status"),
                "final_label": _json(final.get("value")),
                "source_stage": final.get("source_stage"),
                "label_ids": _json(final.get("label_ids")),
                "rule": final.get("rule"),
                "config_version": final.get("config_version"),
                "finalized_at": final.get("at"),
            }
        )
    return rows


def export_labels(tables: Tables, workflow: str) -> list[ExportRow]:
    """One row per label, in submission order."""
    workflow_row(tables, workflow)
    rows = []
    for label in tables.select("labels", workflow=workflow):
        score = tables.get("scores", label=label["label"]) or {}
        rows.append(
            {
                "workflow": workflow,
                "label_id": label["label"],
                "item": label["item"],
                "stage": label["stage"],
                "slot": label["slot"],
                "labeler": label["user"],
                "roles": _json(label["roles"]),
                "config_version": label["config_version"],
                "label_task": label["label_task"],
                "ontology_version": label["ontology_version"],
                "claimed_at": label["claimed_at"],
                "submitted_at": label["submitted_at"],
                "duration_ms": label["duration_ms"],
                "shown_content_hash": label["shown_content_hash"],
                "shown_prior_ids": _json(label["shown_prior_ids"]),
                "show_prior": label["show_prior"],
                "shown_consensus_rule": label["shown_consensus_rule"],
                "score_against": score.get("key_property"),
                "score_key_status": score.get("key_status"),
                "score_agreed": score.get("agreed"),
                "score_rule_version": score.get("rule_version"),
                "label": _json(label["value"]),
            }
        )
    return rows


def _stage_order(tables: Tables, workflow: str) -> dict[str, int]:
    _, config = active_config(tables, workflow)
    return {name: i for i, name in enumerate(config.stage_names)}


def score_report(tables: Tables, workflow: str) -> list[ExportRow]:
    """Per stage with ``score_against`` and per labeler: how many labels agreed with the key.

    ``fraction_agreed`` is agreed / scored, over the labels whose item had a valid key.
    """
    order = _stage_order(tables, workflow)
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for score in tables.select("scores", workflow=workflow):
        groups[(score["stage"], score["key_property"], score["user"])].append(score)
    rows = []
    for (stage, prop, user), scores in sorted(
        groups.items(), key=lambda kv: (order.get(kv[0][0], len(order)), kv[0])
    ):
        scored = [s for s in scores if s["key_status"] == "scored"]
        agreed = sum(1 for s in scored if s["agreed"])
        rows.append(
            {
                "workflow": workflow,
                "stage": stage,
                "score_against": prop,
                "labeler": user,
                "labels": len(scores),
                "scored": len(scored),
                "no_key": len(scores) - len(scored),
                "agreed": agreed,
                "fraction_agreed": agreed / len(scored) if scored else None,
            }
        )
    return rows


def sampling_report(tables: Tables, workflow: str) -> list[ExportRow]:
    """Per stage and sample value: items arrived, admitted, and the actual rate.

    ``sample`` is the threshold the arrivals were compared to. A stage whose ``sample`` an
    edit changed gets one group of rows per value, in the order they were used, so each
    rate sits next to the sample that produced it. Stages are listed in config order, then
    stages that an edit removed.

    The first row of each group counts every arrival (``upstream_labeler`` is None). For a
    stage with an input, further rows split the arrivals by who labeled the item in the
    input stage; an item with several upstream labels counts once for each labeler.
    """
    rows = []
    for stage in stages_ever(tables, workflow):
        arrivals = sorted(
            tables.select("admissions", workflow=workflow, stage=stage.name),
            key=lambda r: r["seq"],
        )
        by_sample: dict[float, list[dict]] = {}
        for arrival in arrivals:
            by_sample.setdefault(arrival["threshold"], []).append(arrival)
        for sample, arrived in (by_sample or {stage.sample: []}).items():
            groups: dict[str | None, list[dict]] = {None: arrived}
            if stage.input_ref is not None:
                upstream = stage.input_ref.stage
                for arrival in arrived:
                    for label in labels_of(tables, workflow, arrival["item"], upstream):
                        groups.setdefault(label["user"], []).append(arrival)
            for labeler in [None, *sorted(k for k in groups if k is not None)]:
                group = groups[labeler]
                admitted = sum(1 for a in group if a["admitted"])
                rows.append(
                    {
                        "workflow": workflow,
                        "stage": stage.name,
                        "input": stage.input,
                        "sample": sample,
                        "upstream_labeler": labeler,
                        "arrived": len(group),
                        "admitted": admitted,
                        "rate": admitted / len(group) if group else None,
                        "slots": sum(a["n_slots"] for a in group),
                    }
                )
    return rows
