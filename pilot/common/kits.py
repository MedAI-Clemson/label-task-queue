"""The pilot kits: what each kit's scripts pass to the shared code."""

from __future__ import annotations

from pathlib import Path

from kit_setup import Kit

PILOT = Path(__file__).resolve().parents[1]

MONTGOMERY = Kit(
    title="Montgomery",
    folder=PILOT / "montgomery",
    project="montgomery_pilot",
    stratify_by="tb_findings",
    gold=12,
    linked_by="same_patient_as",  # prepare.py's item names of the patient's other images
)

JSRT = Kit(
    title="JSRT",
    folder=PILOT / "jsrt",
    project="jsrt_pilot",
    stratify_by="nodule",
    gold=20,
)
