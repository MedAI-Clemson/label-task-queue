"""Pieces every kit's ``prepare.py`` uses: writing the PNGs, checking the gold keys against
the label task, and summarising IoUs. Needs numpy and, for images, Pillow (the ``pilot``
extra: ``pip install -e ".[pilot]"``)."""

from __future__ import annotations

import hashlib
import statistics
from pathlib import Path
from typing import Any

import numpy as np

from labelflow.labeltask import LabelValidationError, OntologyLabelTask


class PrepareError(Exception):
    """The input is not what the script expects; the message says what to fix."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_png(pixels: np.ndarray, path: Path) -> str:
    """Write an 8-bit greyscale PNG and return the SHA-256 of the file."""
    from PIL import Image  # the 'pilot' extra: pip install -e ".[pilot]"

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels, mode="L").save(path, format="PNG")
    return sha256_file(path)


def validate_key(task: OntologyLabelTask, key: dict[str, Any], what: str) -> list[str]:
    """The label task's objections to ``key``, each prefixed with ``what``; empty if none."""
    try:
        task.validate(key)
    except LabelValidationError as error:
        return [f"{what}: {problem}" for problem in error.problems]
    return []


def iou_summary(values: list[float], threshold: float | None = None) -> dict[str, Any]:
    """n, mean, sd, median, 5th percentile and minimum of ``values``, and with a
    ``threshold`` how often it is met."""
    values = sorted(values)
    if not values:
        return {"n": 0} if threshold is None else {"n": 0, "threshold": threshold}
    summary: dict[str, Any] = {"n": len(values)}
    if threshold is not None:
        summary["threshold"] = threshold
    summary |= {
        "mean": round(statistics.fmean(values), 4),
        "sd": round(statistics.stdev(values), 4) if len(values) > 1 else None,
        "median": round(statistics.median(values), 4),
        "p05": round(float(np.percentile(values, 5)), 4),
        "min": round(values[0], 4),
    }
    if threshold is not None:
        summary["fraction_at_or_above_threshold"] = round(
            sum(v >= threshold for v in values) / len(values), 4
        )
    return summary
