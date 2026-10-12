"""Download everything a pilot's audit needs from a running labelflow server.

Each kit's ``export_pilot.py`` calls ``main`` here with its ``Kit``. For each workflow of
the project it writes, in ``--out``:

- ``<workflow>/items.csv`` and ``.parquet``: one row per image (status, final label, its
  source stage, the label ids and the rule that chose them, the config version);
- ``<workflow>/labels.csv`` and ``.parquet``: one row per label (labeler, roles held at the
  claim and at the submit, stage, versions, times, what was shown, the score);
- ``<workflow>/scores.json`` and ``sampling.json``: the score and sampling reports;
- ``<workflow>/versions.json``: every config version, who activated it, why, and its diff;
- ``<workflow>/explain/<item>.txt``: ``explain(item)`` for every image;
- ``<workflow>/structure_scores.csv``: from the ``label_scored`` events, per stage,
  labeler and field (each mask class and each classification): labels scored, how many
  agreed with the key, and the mean and lowest IoU;

and, for the whole project, ``events.jsonl`` (the full event log, oldest first, each event
with its hash chain) and ``roles.json``. The admin password comes from
``LABELFLOW_ADMIN_PASSWORD`` or a prompt. Nothing on the server is changed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kit_setup import Api, Kit, SetupError, admin_password


def all_events(api: Api, base: str) -> Iterator[dict[str, Any]]:
    offset = 0
    while True:
        page = api.get(f"{base}/events?limit=500&offset={offset}")
        yield from page["rows"]
        offset += len(page["rows"])
        if not page["rows"] or offset >= page["total"]:
            return


def structure_scores(events: list[dict[str, Any]], workflow: str) -> list[dict[str, Any]]:
    """Per stage, labeler and field, how the labels compared with the key."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["type"] != "label_scored" or event["workflow"] != workflow:
            continue
        if event["key_status"] != "scored":
            continue
        detail = event["detail"]
        for kind in ("classifications", "masks"):
            for name, field in (detail.get(kind) or {}).items():
                groups[(event["stage"], event["user"], name)].append(field)
    rows = []
    for (stage, user, name), fields in sorted(groups.items()):
        ious = [f["min_iou"] for f in fields if f.get("min_iou") is not None]
        rows.append(
            {
                "stage": stage,
                "labeler": user,
                "field": name,
                "scored": len(fields),
                "agreed": sum(bool(f["agreed"]) for f in fields),
                "threshold": fields[0].get("threshold", ""),
                "mean_iou": round(statistics.fmean(ious), 4) if ious else "",
                "min_iou": round(min(ious), 4) if ious else "",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def export(args: argparse.Namespace, api: Api, password: str, say: Callable[[str], None]) -> None:
    api.login(args.admin, password)
    base = f"/projects/{quote(args.project)}"
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    events = list(all_events(api, base))
    with (out / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    say(f"events.jsonl: {len(events)} events")
    (out / "roles.json").write_text(json.dumps(api.get(f"{base}/roles"), indent=2) + "\n")

    for workflow in [w["workflow"] for w in api.get(f"{base}/workflows")]:
        folder = out / workflow
        (folder / "explain").mkdir(parents=True, exist_ok=True)
        wbase = f"{base}/workflows/{quote(workflow)}"
        for table in ("items", "labels"):
            for kind in ("csv", "parquet"):
                data = api.fetch(f"{wbase}/exports/{table}?format={kind}")
                (folder / f"{table}.{kind}").write_bytes(data)
        for name, path in (
            ("scores", "reports/scores"),
            ("sampling", "reports/sampling"),
            ("versions", "versions"),
        ):
            reply = api.get(f"{wbase}/{path}")
            (folder / f"{name}.json").write_text(json.dumps(reply, indent=2) + "\n")
        items = list(csv.DictReader(io.StringIO(api.fetch(f"{wbase}/exports/items").decode())))
        for row in items:
            reply = api.get(f"{wbase}/items/{quote(row['item'])}/explain")
            (folder / "explain" / f"{row['item']}.txt").write_text(reply["text"] + "\n")
        write_csv(folder / "structure_scores.csv", structure_scores(events, workflow))
        say(f"{workflow}: {len(items)} items exported to {folder}")


def parse_args(kit: Kit, argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"python {kit.path}/export_pilot.py",
        description=f"Download the exports, reports, explanations and event log of the "
        f"{kit.title} pilot. Changes nothing on the server.",
    )
    parser.add_argument("--server", default="http://localhost:8000", help="the server's URL")
    parser.add_argument("--admin", required=True, help="an administrator's username")
    parser.add_argument(
        "--project", default=kit.project, help=f"project name (default: {kit.project})"
    )
    parser.add_argument("--out", type=Path, required=True, help="output folder")
    return parser.parse_args(argv)


def main(
    kit: Kit,
    argv: Sequence[str] | None = None,
    password: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    args = parse_args(kit, argv)
    try:
        export(args, Api(args.server), admin_password(args.admin, password), say)
    except (SetupError, OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
