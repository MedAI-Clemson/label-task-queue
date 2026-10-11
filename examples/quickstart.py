"""Run two example workflows end to end, in memory, with no server and no database.

    python examples/quickstart.py

It uses the real engine and the real chest X-ray ontology (examples/ontologies/chest_xray.yaml),
with simulated labelers standing in for people. Everything is seeded, so the output is the same
on every run. What it prints, in order:

1. examples/workflows/review_sample.yaml: annotators label every image, an expert reviews a 20%
   sample. Progress, the sampling report, the full "why" trace of one reviewed image
   (``explain``), and the items export.
2. examples/workflows/training_round.yaml: trainees label gold-standard images and are scored
   against the key. The score report.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from labelflow.engine import Engine, InMemoryStore
from labelflow.engine.config import validate_config
from labelflow.labeltask import Label, LabelTaskRegistry, load_ontology, rle

EXAMPLES = Path(__file__).resolve().parent
SEED = 2053


# --- a tiny deterministic world --------------------------------------------------------------


class FixedClock:
    """Starts at a set time and moves one minute per reading, so timestamps are reproducible."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(minutes=1)
        return current


def content_hash(item: str) -> str:
    """The SHA-256 the importer would take of the image file. Here, of the file name."""
    return hashlib.sha256(item.encode()).hexdigest()


def true_label(item: str) -> Label:
    """The 'correct' label of an image, in the shape the ontology asks for.

    Odd-numbered images are PA views with an effusion in the lower right; even-numbered
    ones are AP views with no effusion. Masks are 16 x 16 COCO RLEs (see labeltask/rle.py).
    """
    number = int(item.split("_")[1])
    lung = np.zeros((16, 16), dtype=bool)
    lung[2:14, 3:13] = True
    effusion = np.zeros((16, 16), dtype=bool)
    if number % 2:
        effusion[10:14, 8:13] = True
    return {
        "classifications": {"view": "PA" if number % 2 else "AP", "quality": "good"},
        "masks": {"lung": rle.encode(lung), "effusion": rle.encode(effusion)},
    }


def wrong_label(item: str) -> Label:
    """A plausible mistake: the other view, and the effusion mask left empty."""
    label = true_label(item)
    label["classifications"]["view"] = "AP" if label["classifications"]["view"] == "PA" else "PA"
    label["masks"]["effusion"] = rle.encode(np.zeros((16, 16), dtype=bool))
    return label


def simulate(
    engine: Engine,
    workflow: str,
    accuracy: dict[str, float],
    rng: random.Random,
    label_for: Callable[[str, str], Label] | None = None,
) -> None:
    """Let the simulated labelers work until nobody is offered anything.

    Each person keeps asking the workflow for work (``request_work``), looks at what they
    were given (``work_packet``), and submits a label. The label is the true one with
    probability ``accuracy[user]``, else a wrong one. ``label_for(user, item)`` overrides that.
    """
    people = list(accuracy)
    while True:
        worked = False
        for user in rng.sample(people, len(people)):
            slot = engine.request_work(workflow, user)
            if slot is None:
                continue
            packet = engine.work_packet(workflow, slot.slot, user)
            if label_for is not None:
                label = label_for(user, slot.item)
            elif rng.random() < accuracy[user]:
                label = true_label(slot.item)
            else:
                label = wrong_label(slot.item)
            engine.submit(
                workflow,
                slot.slot,
                user,
                label,
                packet.shown(),  # what the interface showed: the image hash, earlier labels
                duration_ms=rng.randint(20_000, 90_000),
            )
            worked = True
        if not worked:
            return


# --- printing --------------------------------------------------------------------------------


def heading(text: str) -> None:
    print(f"\n{'=' * 88}\n{text}\n{'=' * 88}")


def section(text: str) -> None:
    print(f"\n--- {text}\n")


def table(rows: Sequence[dict], columns: Sequence[str]) -> None:
    """Print selected columns of export rows, lined up."""
    cells = [[_cell(row[c]) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    for row in cells:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def progress(engine: Engine, workflow: str) -> None:
    items = Counter(row["status"] for row in engine.export_items(workflow))
    labels = Counter((row["stage"], row["labeler"]) for row in engine.export_labels(workflow))
    print("items:  " + ", ".join(f"{n} {status}" for status, n in sorted(items.items())))
    by_stage: dict[str, list[str]] = {}
    for (stage, user), n in sorted(labels.items()):
        by_stage.setdefault(stage, []).append(f"{user} {n}")
    for stage, parts in by_stage.items():
        print(f"labels: {stage}: {', '.join(parts)}")


# --- the demo --------------------------------------------------------------------------------


def start(engine: Engine, config_file: Path, seed: int) -> str:
    """Validate an example config, start it, and print any staffing warnings."""
    report = validate_config(config_file.read_text())
    assert report.ok, report.render()
    warnings = engine.start_workflow(report.config, seed=seed, actor="admin")
    for warning in warnings:
        print("warning:", warning)
    return report.config.workflow


def review_sample(engine: Engine) -> None:
    heading(
        "1. Annotators label every image; an expert reviews a 20% sample\n"
        "   (examples/workflows/review_sample.yaml)"
    )
    engine.create_dataset("xray_batch_1", actor="admin")
    for n in range(1, 13):
        item = f"img_{n:03d}"
        engine.import_item(
            "xray_batch_1", item, f"file:///data/xray/{item}.png", content_hash(item), actor="admin"
        )
    workflow = start(engine, EXAMPLES / "workflows" / "review_sample.yaml", seed=SEED)

    # The expert always labels PA/AP correctly; annotators get it right 85% of the time.
    simulate(engine, workflow, {"ana": 0.85, "ben": 0.85, "dr_smith": 1.0}, random.Random(7))

    section("progress")
    progress(engine, workflow)

    section("sampling report: was the 20% really 20%? (sampling_report)")
    rows = [r for r in engine.sampling_report(workflow) if r["stage"] == "expert_review"]
    table(rows, ["stage", "sample", "upstream_labeler", "arrived", "admitted", "rate"])

    reviewed = next(
        r["item"] for r in engine.export_items(workflow) if r["source_stage"] == "expert_review"
    )
    section(f"explain({reviewed!r}): one image that was sampled for review, step by step")
    print(engine.explain(workflow, reviewed).render(), end="")

    rows = engine.export_items(workflow)
    section(f"items export: one row per image (4 of its {len(rows[0])} columns; export_items)")
    for row in rows:
        row["final view"] = json.loads(row["final_label"])["classifications"]["view"]
    table(rows, ["item", "status", "source_stage", "final view", "rule"])


def training_round(engine: Engine) -> None:
    heading(
        "2. Trainees label gold-standard images and are scored against the key\n"
        "   (examples/workflows/training_round.yaml)"
    )
    engine.create_dataset("gold_set_1", actor="admin")
    for n in range(1, 9):
        item = f"gold_{n:03d}"
        engine.import_item(
            "gold_set_1",
            item,
            f"file:///data/gold/{item}.png",
            content_hash(item),
            properties={"gold_key": true_label(item)},  # the expert's answer, never shown
            actor="admin",
        )
    workflow = start(engine, EXAMPLES / "workflows" / "training_round.yaml", seed=SEED)
    simulate(engine, workflow, {"lee": 1.0, "mia": 0.75, "noor": 0.5}, random.Random(3))

    section("progress")
    progress(engine, workflow)

    section("score report: each trainee's agreement with the gold key (score_report)")
    table(
        engine.score_report(workflow),
        ["stage", "labeler", "labels", "scored", "agreed", "fraction_agreed"],
    )

    section("one scored label, from the labels export (export_labels)")
    row = next(r for r in engine.export_labels(workflow) if r["labeler"] == "noor")
    for column in (
        "label_id",
        "item",
        "stage",
        "labeler",
        "roles",
        "score_against",
        "score_agreed",
        "shown_content_hash",
    ):
        print(f"{column:>20}: {row[column]}")


def main() -> None:
    # One Engine is one project: its roles, people, datasets and workflows. The in-memory store
    # keeps the same append-only event log the SQLite store does; nothing here is written to disk.
    ontology = load_ontology(EXAMPLES / "ontologies" / "chest_xray.yaml")
    engine = Engine(InMemoryStore(), LabelTaskRegistry([ontology]), clock=FixedClock())

    # Roles are project state. A workflow config only names them.
    for role in ("annotator", "expert", "trainee"):
        engine.define_role(role, actor="admin")
    for user, role in [("ana", "annotator"), ("ben", "annotator"), ("dr_smith", "expert")]:
        engine.grant_role(user, role, actor="admin", reason="project start")
    for user in ("lee", "mia", "noor"):
        engine.grant_role(user, "trainee", actor="admin", reason="training cohort 1")

    review_sample(engine)
    training_round(engine)

    events = engine.store.events()
    print(
        f"\nThe project's event log holds {len(events)} events; every line above was read from it."
    )


if __name__ == "__main__":
    main()
