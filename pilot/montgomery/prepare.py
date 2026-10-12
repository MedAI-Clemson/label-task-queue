"""Turn the Montgomery County chest X-ray set into PNGs, gold keys and an import file.

    python pilot/montgomery/prepare.py --montgomery DIR --out DIR

What it reads (the unzipped download; nothing is fetched): ``--montgomery`` is the
``MontgomerySet`` folder of ``NLM-MontgomeryCXRSet.zip``, or any folder above it. Files are
found at any depth by their names, ``MCUCXR_<number>_<0|1>``, where the last digit is the
dataset's label (0 normal, 1 manifestation of tuberculosis):

- ``CXR_png/MCUCXR_*.png``: the radiographs, 8-bit greyscale PNG (the ReadMe says 12-bit,
  but the files are 8-bit), 4020 x 4892 (97 images) or 4892 x 4020 (41), already in display
  polarity (bone bright);
- ``ManualMask/leftMask/MCUCXR_*.png`` and ``ManualMask/rightMask/...``: the lung masks,
  1-bit PNG at the image's size. The folders are named by IMAGE side: ``leftMask`` holds
  the lung on the left of the image, which is the PATIENT's right lung. Each image is
  checked: the patient's right lung must lie left of the left lung on the image;
- ``ClinicalReadings/MCUCXR_*.txt``: ``Patient's Sex: F``, ``Patient's Age: 031Y`` and the
  reading, in LF or CRLF lines; some readings say which other images are of the same
  patient ("same pt as MCUCXR_0113_1").

Files the Mac zip tool added (``__MACOSX/``, ``._*``, ``.DS_Store``, ``Thumbs.db``) are
ignored.

What it writes in ``--out``:

- ``png/<item>.png``: 8-bit greyscale, the longest side ``--side`` pixels (default 1024: 841
  x 1024 or 1024 x 841), shrunk by area averaging. The masks are shrunk the same way, a
  pixel being in the mask when at least half of it was, so they stay aligned.
- ``items.json``: the import file, a JSON list of items as ``POST .../datasets/{d}/items``
  takes them: ``item``, ``uri`` (a ``file://`` address in the server's media folder),
  ``content_hash`` (the PNG's SHA-256), and ``properties``: ``gold_key`` (the two masks and
  the TB answer), ``tb_findings``, the clinical data and the source file and its hash.
- ``report.json`` and ``report.txt``: counts, image sizes, lung areas, how much the
  shrinking changed the masks, and the readings worth knowing about.

**Item names hide the answer.** The dataset's file names give the TB answer (the last digit)
and so do its numbers (every normal image is numbered 0001-0103, every TB image 0104 and
up), and a labeler sees the item name. So each item is named ``mc_`` plus the first ten hex
digits of the SHA-256 of its source PNG, and the original name is kept as the property
``source_id``, which labelers never see. ``items.json`` lists the items in the order of
these names, not the dataset's.

Every key is validated with the ontology's label task before anything is written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from labelflow.labeltask import load_ontology, rle

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "common"))

from kit_prepare import (  # noqa: E402
    PrepareError,
    iou_summary,
    sha256_file,
    validate_key,
    write_png,
)

DEFAULT_ONTOLOGY = HERE / "ontology" / "montgomery_tb.yaml"
SIDE = 1024
SOURCE_ID = re.compile(r"^MCUCXR_(\d{4})_([01])$")
TB_ANSWER = {"0": "absent", "1": "present"}
# The dataset's mask folders, named by image side, and the patient's lung each holds.
MASK_FOLDERS = {"leftmask": "right_lung", "rightmask": "left_lung"}
STRUCTURES = ("right_lung", "left_lung")
SAME_PATIENT = re.compile(r"same pt as ([A-Z0-9_,\s]+)", re.IGNORECASE)
# Readings of '_1' images that describe old or inactive disease, or no reading at all.
NOTEWORTHY = re.compile(
    r"\b(inactive|stable|no report|old|healed|scars?|calcif\w*|fibrosis)\b", re.IGNORECASE
)


# --- Finding the files ----------------------------------------------------------------------


@dataclass
class Source:
    """One radiograph of the dataset and its files."""

    source_id: str  # MCUCXR_0001_0
    image: Path | None = None
    reading: Path | None = None
    masks: dict[str, Path] | None = None

    @property
    def tb_findings(self) -> str:
        return TB_ANSWER[self.source_id[-1]]


def find_sources(root: Path) -> dict[str, Source]:
    """Every ``MCUCXR_*`` file under ``root``, grouped by radiograph."""
    if not root.is_dir():
        raise PrepareError(f"There is no folder at {root}.")
    sources: dict[str, Source] = {}

    def put(source: Source, attribute: str, path: Path) -> None:
        if getattr(source, attribute) is not None:
            raise PrepareError(
                f"{source.source_id}: two files for the same thing, {getattr(source, attribute)} "
                f"and {path}. Point --montgomery at one copy of the dataset."
            )
        setattr(source, attribute, path)

    for path in sorted(root.rglob("*")):
        if "__MACOSX" in path.parts or not SOURCE_ID.match(path.stem) or not path.is_file():
            continue
        source = sources.setdefault(path.stem, Source(path.stem))
        folder = path.parent.name.lower()
        suffix = path.suffix.lower()
        if suffix == ".txt":
            put(source, "reading", path)
        elif suffix == ".png" and folder in MASK_FOLDERS:
            masks = source.masks if source.masks is not None else {}
            structure = MASK_FOLDERS[folder]
            if structure in masks:
                raise PrepareError(f"{source.source_id}: two masks in {folder}: {path}.")
            masks[structure] = path
            source.masks = masks
        elif suffix == ".png":
            put(source, "image", path)
    if not sources:
        raise PrepareError(
            f"No Montgomery files (MCUCXR_*.png, MCUCXR_*.txt) were found under {root}."
        )
    return sources


def check_complete(sources: dict[str, Source]) -> None:
    problems = []
    numbers: dict[str, str] = {}
    for source in sources.values():
        missing = [
            what
            for what, there in (
                ("image", source.image),
                ("clinical reading", source.reading),
                *((f"{s} mask", (source.masks or {}).get(s)) for s in STRUCTURES),
            )
            if there is None
        ]
        if missing:
            problems.append(f"{source.source_id}: no {', '.join(missing)}.")
        number = source.source_id.split("_")[1]
        if number in numbers:
            problems.append(f"{source.source_id} and {numbers[number]} have the same number.")
        numbers[number] = source.source_id
    if problems:
        raise PrepareError("\n".join(["The files do not fit together:", *problems]))


# --- Reading them ---------------------------------------------------------------------------


def parse_reading(path: Path) -> dict[str, Any]:
    """Sex, age (whole years) and the reading's text of a ``ClinicalReadings`` file, and the
    source ids of other images of the same patient that the reading names."""
    text = path.read_bytes().decode("latin-1")
    lines = [line.strip() for line in text.splitlines()]
    record: dict[str, Any] = {"sex": None, "age": None}
    rest = []
    for line in lines:
        key, _, value = line.partition(":")
        if key.strip().lower() == "patient's sex":
            record["sex"] = value.strip() or None
        elif key.strip().lower() == "patient's age":
            match = re.fullmatch(r"0*(\d+)\s*Y", value.strip(), re.IGNORECASE)
            if not match:
                raise PrepareError(f"{path.name}: cannot read the age {value.strip()!r}.")
            record["age"] = int(match.group(1))
        elif line:
            rest.append(line)
    if record["age"] is None or record["sex"] is None:
        raise PrepareError(f"{path.name}: no 'Patient's Sex' or 'Patient's Age' line.")
    record["reading"] = " ".join(rest)
    same = []
    for match in SAME_PATIENT.finditer(record["reading"]):
        same += re.findall(r"MCUCXR_\d{4}_[01]", match.group(1), re.IGNORECASE)
    record["same_patient_as"] = [s.upper() for s in same]
    return record


def read_image(path: Path) -> np.ndarray:
    """A radiograph as 8-bit grey. The files are 8-bit; should a 16-bit one turn up, it is
    windowed between its own 0.5th and 99.5th percentiles."""
    from PIL import Image

    with Image.open(path) as image:
        if image.mode == "L":
            return np.asarray(image).copy()
        if image.mode in ("I;16", "I;16B", "I"):
            values = np.asarray(image).astype(np.float64)
            low, high = (float(v) for v in np.percentile(values, [0.5, 99.5]))
            high = max(high, low + 1.0)
            return np.rint((np.clip(values, low, high) - low) / (high - low) * 255).astype(np.uint8)
        return np.asarray(image.convert("L")).copy()


def read_mask(path: Path, shape: tuple[int, int]) -> np.ndarray:
    """A mask image as a boolean array of the image's ``shape``; foreground is non-zero."""
    from PIL import Image

    with Image.open(path) as image:
        mask = np.asarray(image.convert("L")) > 0
    if mask.shape != shape:
        raise PrepareError(
            f"{path} is {mask.shape[1]} x {mask.shape[0]}, but its image is "
            f"{shape[1]} x {shape[0]}."
        )
    return mask


# --- Shrinking ------------------------------------------------------------------------------


def display_size(width: int, height: int, side: int = SIDE) -> tuple[int, int]:
    """(width, height) with the longest side ``side`` and the aspect ratio kept."""
    scale = side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def shrink_image(pixels: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Area-average an 8-bit image to ``size`` (width, height)."""
    from PIL import Image

    return np.asarray(Image.fromarray(pixels, mode="L").resize(size, Image.Resampling.BOX))


def shrink_mask(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Area-average a mask to ``size`` (width, height) with the same box filter as the image;
    a pixel is in the mask when at least half of its area was."""
    from PIL import Image

    covered = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    return np.asarray(covered.resize(size, Image.Resampling.BOX)) >= 128


def resize_iou(original: np.ndarray, shrunk: np.ndarray) -> float:
    """IoU between the original mask and the shrunk one blown back up (nearest pixel): how
    much the display size alone moves the outline."""
    from PIL import Image

    height, width = original.shape
    back = Image.fromarray(shrunk.astype(np.uint8) * 255, mode="L")
    back = np.asarray(back.resize((width, height), Image.Resampling.NEAREST)) > 0
    union = np.logical_or(original, back).sum()
    return float(np.logical_and(original, back).sum() / union) if union else 1.0


def centre_column(mask: np.ndarray) -> float:
    return float(np.nonzero(mask)[1].mean()) if mask.any() else float("nan")


def item_name(source_sha256: str) -> str:
    return f"mc_{source_sha256[:10]}"


def build_key(masks: dict[str, np.ndarray], tb_findings: str) -> dict[str, Any]:
    """A label in the ontology's format: the TB answer and one RLE per lung."""
    return {
        "classifications": {"tb_findings": tb_findings},
        "masks": {name: rle.encode(masks[name]) for name in STRUCTURES},
    }


# --- The command ----------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    task = load_ontology(args.ontology)
    png_dir = args.out / "png"
    media_root = (args.media_root or png_dir).resolve()

    sources = find_sources(args.montgomery)
    ids = sorted(sources)[: args.limit] if args.limit else sorted(sources)
    check_complete({i: sources[i] for i in ids})

    items, report_rows, problems = [], [], []
    for count, source_id in enumerate(ids, start=1):
        source = sources[source_id]
        record = parse_reading(source.reading)
        pixels = read_image(source.image)
        height, width = pixels.shape
        masks = {s: read_mask(source.masks[s], (height, width)) for s in STRUCTURES}
        empty = [s for s in STRUCTURES if not masks[s].any()]
        if empty:
            problems.append(f"{source_id}: the {', '.join(empty)} mask is empty.")
            continue
        if not centre_column(masks["right_lung"]) < centre_column(masks["left_lung"]):
            problems.append(
                f"{source_id}: the patient's right lung (leftMask) is not on the left of the "
                "image. Are the mask folders swapped, or the image flipped?"
            )
            continue

        size = display_size(width, height, args.side)
        shrunk = {s: shrink_mask(masks[s], size) for s in STRUCTURES}
        key = build_key(shrunk, source.tb_findings)
        found = validate_key(task, key, f"{source_id} gold_key")
        if found:
            problems += found
            continue

        source_sha256 = sha256_file(source.image)
        name = item_name(source_sha256)
        png = png_dir / f"{name}.png"
        digest = write_png(shrink_image(pixels, size), png)
        properties: dict[str, Any] = {
            "gold_key": key,
            "tb_findings": source.tb_findings,
            "sex": record["sex"],
            "age": record["age"],
            "reading": record["reading"],
            "same_patient_as": record["same_patient_as"],  # source ids; mapped below
            "source_id": source_id,
            "source_file": source.image.name,
            "source_sha256": source_sha256,
            "source_size": [width, height],
            "gold_source": "Montgomery County CXR set (NLM): ManualMask (leftMask is the "
            "patient's right lung) and the TB label of the file name",
        }
        items.append(
            {
                "item": name,
                "uri": (media_root / png.name).as_uri(),
                "content_hash": digest,
                "properties": properties,
            }
        )
        report_rows.append(
            {
                "item": name,
                "source_id": source_id,
                "tb_findings": source.tb_findings,
                "source_size": (width, height),
                "display_size": size,
                "area": {s: float(shrunk[s].mean()) for s in STRUCTURES},
                "resize_iou": {s: resize_iou(masks[s], shrunk[s]) for s in STRUCTURES},
                "overlap": int(np.logical_and(*shrunk.values()).sum()),
                "reading": record["reading"],
            }
        )
        if not args.quiet and count % 25 == 0:
            print(f"  {count} of {len(ids)} images done", file=sys.stderr)
    if problems:
        raise PrepareError("\n".join(["Nothing was written, because:", *problems]))

    names = {item["properties"]["source_id"]: item["item"] for item in items}
    for item in items:
        same = item["properties"]["same_patient_as"]
        item["properties"]["same_patient_as"] = [names[s] for s in same if s in names]
    if len({item["item"] for item in items}) != len(items):
        raise PrepareError("Two images have the same content; is a file there twice?")
    # In the order of the opaque names: the source order puts every normal image first.
    items.sort(key=lambda item: item["item"])

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "items.json").write_text(json.dumps(items, indent=1) + "\n")
    report = build_report(report_rows, args, media_root)
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.out / "report.txt").write_text(render_report(report) + "\n")
    return report


def build_report(rows: list[dict[str, Any]], args: argparse.Namespace, media_root: Path) -> dict:
    noteworthy = [
        {"item": r["item"], "source_id": r["source_id"], "reading": r["reading"]}
        for r in rows
        if r["tb_findings"] == "present" and NOTEWORTHY.search(r["reading"])
    ]
    return {
        "images": len(rows),
        "tb_findings": dict(sorted(Counter(r["tb_findings"] for r in rows).items())),
        "source_sizes": {
            f"{w} x {h}": n for (w, h), n in sorted(Counter(r["source_size"] for r in rows).items())
        },
        "display_sizes": {
            f"{w} x {h}": n
            for (w, h), n in sorted(Counter(r["display_size"] for r in rows).items())
        },
        "lung_area_fraction": {s: iou_summary([r["area"][s] for r in rows]) for s in STRUCTURES},
        "resize_iou": {s: iou_summary([r["resize_iou"][s] for r in rows]) for s in STRUCTURES},
        "images_where_the_lungs_overlap": sum(r["overlap"] > 0 for r in rows),
        "tb_readings_old_inactive_or_missing": noteworthy,
        "settings": {
            "side": args.side,
            "ontology": str(args.ontology),
            "media_root": str(media_root),
        },
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"{report['images']} images: "
        + ", ".join(f"{n} tb_findings={k}" for k, n in report["tb_findings"].items()),
        "Source sizes:  " + "; ".join(f"{k}: {n}" for k, n in report["source_sizes"].items()),
        "Display sizes: " + "; ".join(f"{k}: {n}" for k, n in report["display_sizes"].items()),
        "",
        "Per lung, over all images:",
        f"  {'':<12}{'area of image':>16}{'min':>8}   {'resize IoU':>12}{'min':>8}",
    ]
    for s in STRUCTURES:
        area, iou = report["lung_area_fraction"][s], report["resize_iou"][s]
        lines.append(
            f"  {s:<12}{area['mean']:>16.3f}{area['min']:>8.3f}   {iou['mean']:>12.4f}"
            f"{iou['min']:>8.4f}"
        )
    lines += [
        "  (resize IoU: the original mask against the display-size mask blown back up; how",
        "  much the shrinking alone moves the outline. It should be far above the threshold.)",
        f"Images where the two lung masks overlap at display size: "
        f"{report['images_where_the_lungs_overlap']}",
        "",
        "TB images whose reading describes old, inactive or stable disease, or has none",
        "(their gold answer is still 'present'; the dataset counts them as TB manifestations):",
    ]
    for row in report["tb_readings_old_inactive_or_missing"]:
        lines.append(f"  {row['item']} ({row['source_id']}): {row['reading'][:110]}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python pilot/montgomery/prepare.py",
        description="Convert the Montgomery County chest X-ray set into PNGs, gold keys and an "
        "import file for the labelflow pilot. See pilot/montgomery/README.md.",
    )
    parser.add_argument(
        "--montgomery",
        type=Path,
        required=True,
        help="the unzipped MontgomerySet folder (or a folder above it)",
    )
    parser.add_argument("--out", type=Path, required=True, help="output folder")
    parser.add_argument(
        "--media-root",
        type=Path,
        help="the folder the server will read the PNGs from (LABELFLOW_MEDIA_ROOT), if you "
        "will move them there; the items' uris point into it (default: OUT/png)",
    )
    parser.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY, help="ontology file")
    parser.add_argument(
        "--side",
        type=int,
        default=SIDE,
        help=f"the longest side of the PNGs, in pixels ({SIDE}); the ontology's "
        "max_mask_pixels must allow it",
    )
    parser.add_argument("--limit", type=int, help="only the first N images (to try it out)")
    parser.add_argument("--quiet", action="store_true", help="print only errors")
    args = parser.parse_args(argv)
    if args.side < 16:
        parser.error("--side must be at least 16")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = prepare(args)
    except PrepareError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"Wrote {report['images']} PNGs to {args.out / 'png'}")
        print(f"Wrote the import file {args.out / 'items.json'}")
        print()
        print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
