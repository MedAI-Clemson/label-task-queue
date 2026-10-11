"""Check an export written by run_check.mjs against the project's own label task and RLE code.

Run with the project venv (see check.sh):

    python spikes/mask-tools/check/check_export.py spikes/mask-tools/check/out/export-index.json

It checks that the submit body
  * validates with OntologyLabelTask (examples/ontologies/chest_xray.yaml), and `shown` and
    `duration_ms` are in the shape the submit endpoint expects;
  * decodes identically from compressed and uncompressed counts with labelflow.labeltask.rle;
  * matches, pixel for pixel, a numpy re-implementation of the documented rasterization rules
    (pixel centers; disk / capsule for the brush; even-odd polygon) replayed over the page's
    op log, undo and redo included;
  * for the known shapes, has the expected areas: an 80 x 50 rectangle gives 4000 pixels, the
    20 x 20 polygon erase removes 400, and the brush dot of diameter 11 at an integer point
    covers the 97 pixel centers within 5.5 of it.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

from labelflow.labeltask import rle
from labelflow.labeltask.ontology_task import load_ontology

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY = ROOT / "examples" / "ontologies" / "chest_xray.yaml"


# --- The rules, written a second time in numpy ---------------------------------------------


def centers(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:height, 0:width].astype(np.float64)
    return x + 0.5, y + 0.5


def disk(cx: np.ndarray, cy: np.ndarray, px: float, py: float, r: float) -> np.ndarray:
    dx = cx - px
    dy = cy - py
    return dx * dx + dy * dy <= r * r


def capsule(cx, cy, ax, ay, bx, by, r) -> np.ndarray:
    ex, ey = bx - ax, by - ay
    len2 = ex * ex + ey * ey
    if len2 == 0:
        return disk(cx, cy, ax, ay, r)
    dx, dy = cx - ax, cy - ay
    dot = dx * ex + dy * ey
    near_a = dx * dx + dy * dy <= r * r
    fx, fy = cx - bx, cy - by
    near_b = fx * fx + fy * fy <= r * r
    cross = ex * dy - ey * dx
    near_segment = cross * cross <= (r * r) * len2
    return np.where(dot <= 0, near_a, np.where(dot >= len2, near_b, near_segment))


def stroke(cx, cy, points, diameter) -> np.ndarray:
    r = diameter / 2
    points = [(np.floor(x) + 0.5, np.floor(y) + 0.5) for x, y in points]  # pixel centers
    if len(points) == 1:
        return disk(cx, cy, points[0][0], points[0][1], r)
    out = np.zeros(cx.shape, dtype=bool)
    for (ax, ay), (bx, by) in itertools.pairwise(points):
        out |= capsule(cx, cy, ax, ay, bx, by, r)
    return out


def polygon(cx, cy, vertices) -> np.ndarray:
    """Even-odd rule at pixel centers, with the same half-open edge test and the same
    expression for the crossing x as maskcore.js (so the floating point agrees)."""
    inside = np.zeros(cx.shape, dtype=bool)
    n = len(vertices)
    for i in range(n):
        x0, y0 = vertices[i]
        x1, y1 = vertices[(i + 1) % n]
        crosses = (y0 <= cy) != (y1 <= cy)
        if not crosses.any():
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = x0 + (cy - y0) * (x1 - x0) / (y1 - y0)
        inside ^= crosses & (cx < x_at)
    return inside


def replay(ops: list[dict], classes: list[str], height: int, width: int) -> dict[str, np.ndarray]:
    cx, cy = centers(height, width)
    masks = {name: np.zeros((height, width), dtype=bool) for name in classes}
    undo: list[tuple[str, np.ndarray]] = []
    redo: list[tuple[str, np.ndarray]] = []
    for op in ops:
        if op["kind"] == "undo":
            name, before = undo.pop()
            redo.append((name, masks[name].copy()))
            masks[name] = before
            continue
        if op["kind"] == "redo":
            name, after = redo.pop()
            undo.append((name, masks[name].copy()))
            masks[name] = after
            continue
        name = op["class"]
        undo.append((name, masks[name].copy()))
        redo.clear()
        if op["kind"] == "stroke":
            shape = stroke(cx, cy, op["points"], op["diameter"])
        elif op["kind"] == "polygon":
            shape = polygon(cx, cy, op["vertices"])
        else:
            raise ValueError(f"unknown op {op['kind']}")
        if op["erase"]:
            masks[name] &= ~shape
        else:
            masks[name] |= shape
    return masks


# --- The checks --------------------------------------------------------------------------------


def main(path: str) -> int:
    export = json.loads(Path(path).read_text())
    submit, uncompressed, ops = export["submit"], export["submitUncompressed"], export["opLog"]
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        print(("ok   " if condition else "FAIL ") + message)
        if not condition:
            failures.append(message)

    task = load_ontology(ONTOLOGY)
    for name, body in (("compressed", submit), ("uncompressed", uncompressed)):
        try:
            task.validate(body["label"])
            check(True, f"{name} label validates against {task.ontology_version}")
        except Exception as error:
            check(False, f"{name} label validates: {error}")
    check(
        isinstance(submit["duration_ms"], int) and submit["duration_ms"] >= 0,
        f"duration_ms is {submit['duration_ms']}",
    )
    # The shape of labelflow.engine.events.Shown (not imported: the engine package is being
    # edited in parallel; the label task package is enough for this check).
    shown = submit["shown"]
    check(
        set(shown) == {"content_hash", "prior_label_ids", "show_prior", "consensus_rule_version"},
        f"shown has exactly Shown's fields: {sorted(shown)}",
    )
    check(
        shown["show_prior"] is True and shown["prior_label_ids"] == [101],
        f"shown reports the prior label: {shown}",
    )
    check(
        shown["content_hash"].startswith("sha256:")
        and shown["consensus_rule_version"] == "ontology-consensus@1",
        "shown carries the packet's hash and rule version",
    )
    check(
        submit["label"]["classifications"] == {"view": "PA", "quality": "poor"},
        f"classifications {submit['label']['classifications']}",
    )

    masks = {name: rle.decode(m) for name, m in submit["label"]["masks"].items()}
    for name, m in submit["label"]["masks"].items():
        check(isinstance(m["counts"], str), f"{name}: compressed counts are a string")
        same = np.array_equal(masks[name], rle.decode(uncompressed["label"]["masks"][name]))
        check(same, f"{name}: compressed and uncompressed counts decode to the same pixels")
        reencoded = rle.encode(masks[name])
        check(reencoded == m, f"{name}: re-encoding with labelflow gives byte-identical RLE")

    height, width = submit["label"]["masks"]["lung"]["size"]
    expected = replay(ops, list(masks), height, width)
    for name in masks:
        differing = int(np.count_nonzero(masks[name] != expected[name]))
        check(
            differing == 0,
            f"{name}: browser pixels == numpy replay of {len(ops)} ops "
            f"({differing} differ; area {int(masks[name].sum())})",
        )

    # The known shapes, independently of the replay.
    strokes = [op for op in ops if op["kind"] == "stroke" and not op["erase"]]
    dot = strokes[0]
    cx, cy = centers(height, width)
    dot_pixels = disk(cx, cy, dot["points"][0][0], dot["points"][0][1], dot["diameter"] / 2)
    check(
        dot["points"] == [[100.5, 100.5]] and dot["diameter"] == 11,
        f"first op is the brush dot at {dot['points']} "
        f"(pixel center under the click) d={dot['diameter']}",
    )
    check(
        int(dot_pixels.sum()) == 97 and masks["lung"][dot_pixels].all(),
        f"brush dot covers its {int(dot_pixels.sum())} centers (expected 97)",
    )
    rect = next(
        op for op in ops if op["kind"] == "polygon" and len(op["vertices"]) == 4 and not op["erase"]
    )
    rect_pixels = polygon(cx, cy, rect["vertices"])
    check(
        int(rect_pixels.sum()) == 4000,
        f"rectangle polygon rasterizes to {int(rect_pixels.sum())} px (expected 4000)",
    )
    erase = next(op for op in ops if op["kind"] == "polygon" and op["erase"])
    hole = polygon(cx, cy, erase["vertices"])
    check(
        int(hole.sum()) == 400 and not masks["lung"][hole].any(), "polygon erase removed its 400 px"
    )
    check(masks["lung"][rect_pixels & ~hole].all(), "rectangle minus the hole is all foreground")
    triangle = next(op for op in ops if op["kind"] == "polygon" and len(op["vertices"]) == 3)
    tri_pixels = polygon(cx, cy, triangle["vertices"])
    check(
        np.array_equal(masks["effusion"], tri_pixels),
        f"effusion is exactly the triangle ({int(tri_pixels.sum())} px)",
    )

    print(f"timings reported by the page: {export.get('timings')}")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
