"""Turn the JSRT radiographs and the SCR segmentations into PNGs and an import file.

    python pilot/jsrt/prepare.py --jsrt DIR --scr-points DIR --out DIR

What it reads (folders you downloaded; nothing is fetched):

- ``--jsrt``: the JSRT images, ``JPCLN001.IMG`` ... ``JPCLN154.IMG`` (with a nodule) and
  ``JPCNN001.IMG`` ... ``JPCNN093.IMG`` (without), found in any subfolder. Each is 2048 x
  2048 pixels of 12-bit grey, stored as big-endian 16-bit words with no header (exactly
  8,388,608 bytes). The clinical data files ``CLNDAT_EN.txt`` (nodule cases) and
  ``CNNDAT_EN.TXT`` (non-nodule cases) are looked for in the same tree, or given with
  ``--metadata``.
- The SCR first observer's segmentations (the reference standard), in one of two forms:

  - ``--scr-points``: the ``points`` folder of the SCR record on Zenodo (``points.zip``,
    CC BY 4.0): one ``.pfs`` text file per image with the clicked outline of each of the
    five structures, in the coordinates of a 1024 x 1024 image. Each outline is filled as
    a polygon, a pixel being inside when its centre is (even-odd rule, pixel centres at
    whole-number coordinates). On the SCR masks this reproduces the published masks to
    within a few boundary pixels.
  - ``--scr-masks``: binary mask images (GIF, PNG or TIFF) at 1024 x 1024, one per image
    and structure, in folders named after the structure (``left lung``, ``right lung``,
    ``heart``, ``left clavicle``, ``right clavicle``), as in the original SCR download
    (``fold1/masks/left lung/JPCLN001.gif``).

- ``--scr-points2`` or ``--scr-masks2`` (optional): the second observer's segmentations,
  laid out the same way. They are not in the public downloads; ask the SCR authors.

What it writes in ``--out``:

- ``png/<id>.png``: 8-bit greyscale, 1024 x 1024, the grid of the segmentations (see
  ``to_display`` for the downsampling and the grey-level window).
- ``items.json``: the import file, a JSON list of items as ``POST .../datasets/{d}/items``
  takes them: ``item``, ``uri`` (a ``file://`` address in the server's media folder),
  ``content_hash`` (the PNG's SHA-256), and ``properties``: ``gold_key`` (a label: the
  first observer's five masks plus the nodule answer from the JSRT data),
  ``observer2_key`` where the second observer's segmentations were given, and the JSRT
  clinical data as plain values.
- ``observer_report.json`` and ``.txt``: per structure, the IoU between the two observers
  as the ontology's own consensus check computes it, and how often it meets the threshold.

Every key is validated with the ontology's label task before anything is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from labelflow.labeltask import LabelValidationError, OntologyLabelTask, load_ontology, rle

HERE = Path(__file__).resolve().parent
DEFAULT_ONTOLOGY = HERE / "ontology" / "jsrt_scr.yaml"

RAW_SIDE = 2048
RAW_BYTES = RAW_SIDE * RAW_SIDE * 2
RAW_MAX = 4095  # 12-bit
SIDE = 1024  # the SCR grid: the segmentations, and so the PNGs, are 1024 x 1024
IMAGE_ID = re.compile(r"^JPC(LN|NN)\d{3}$")

# The SCR structures as their files name them (lowercase, words joined by '_'); each
# becomes the mask class of the same name. 'left' is the patient's left, which is on the
# right of a PA image as displayed.
STRUCTURES = ("right_lung", "left_lung", "heart", "right_clavicle", "left_clavicle")


class PrepareError(Exception):
    """The input is not what the script expects; the message says what to fix."""


# --- The images ---------------------------------------------------------------------------


def read_jsrt_raw(path: Path) -> tuple[np.ndarray, int]:
    """A JSRT ``.IMG`` file as a 2048 x 2048 array of 12-bit values, and how many pixels
    were above 4095 and set to 4095.

    The file is raw pixels, row by row, two bytes each, most significant byte first, with no
    header. In the stored values, higher means darker: air is high, bone low (see
    ``to_display``).
    """
    data = path.read_bytes()
    if len(data) != RAW_BYTES:
        raise PrepareError(
            f"{path} has {len(data)} bytes; a JSRT image has exactly {RAW_BYTES} "
            f"({RAW_SIDE} x {RAW_SIDE} pixels of 2 bytes, no header)."
        )
    pixels = np.frombuffer(data, dtype=">u2").reshape(RAW_SIDE, RAW_SIDE)
    above = int((pixels > RAW_MAX).sum())
    if above > pixels.size // 100:
        raise PrepareError(
            f"{path}: {above} pixels are above the 12-bit maximum {RAW_MAX}. Is it a JSRT "
            "image? (They are big-endian; little-endian reading gives values like this.)"
        )
    return np.minimum(pixels, RAW_MAX).astype(np.uint16), above


def to_display(
    raw: np.ndarray,
    side: int = SIDE,
    invert: bool = True,
    window: str = "percentile",
    low_pct: float = 0.5,
    high_pct: float = 99.5,
) -> np.ndarray:
    """The 8-bit image a labeler sees, ``side`` x ``side``.

    1. Downsample by averaging each block of k x k pixels (k = 2048 / 1024 = 2), so pixel
       (r, c) of the PNG covers exactly pixel (r, c) of the segmentations.
    2. Invert (unless ``invert`` is False) so bone is bright and air dark, as radiographs
       are read: ``4095 - value``.
    3. Window: ``percentile`` maps the image's own ``low_pct`` and ``high_pct`` percentiles
       to 0 and 255 and clips beyond them; ``full`` maps the whole range 0..4095.
    4. Round to whole numbers 0..255.
    """
    if raw.shape[0] != raw.shape[1] or raw.shape[0] % side:
        raise PrepareError(f"Cannot downsample a {raw.shape} image to {side} x {side}.")
    k = raw.shape[0] // side
    image = raw.astype(np.float64).reshape(side, k, side, k).mean(axis=(1, 3))
    if invert:
        image = RAW_MAX - image
    if window == "percentile":
        low, high = (float(v) for v in np.percentile(image, [low_pct, high_pct]))
    elif window == "full":
        low, high = 0.0, float(RAW_MAX)
    else:
        raise PrepareError(f"Unknown window {window!r}; use 'percentile' or 'full'.")
    high = max(high, low + 1.0)  # a flat image: avoid dividing by zero
    scaled = (np.clip(image, low, high) - low) / (high - low) * 255.0
    return np.rint(scaled).astype(np.uint8)


def write_png(pixels: np.ndarray, path: Path) -> str:
    """Write an 8-bit greyscale PNG and return the SHA-256 of the file."""
    from PIL import Image  # the 'pilot' extra: pip install -e ".[pilot]"

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels, mode="L").save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_images(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise PrepareError(f"There is no folder at {root}.")
    images: dict[str, Path] = {}
    for path in sorted(p for p in root.rglob("*") if p.suffix.lower() == ".img"):
        image_id = path.stem.upper()
        if IMAGE_ID.match(image_id):
            if image_id in images:
                raise PrepareError(f"{image_id} is found twice: {images[image_id]} and {path}.")
            images[image_id] = path
    if not images:
        raise PrepareError(f"No JSRT images (JPCLN*.IMG, JPCNN*.IMG) were found under {root}.")
    return images


# --- The clinical data ----------------------------------------------------------------------


def find_metadata(root: Path) -> list[Path]:
    names = {"clndat_en.txt", "cnndat_en.txt"}
    return sorted(p for p in root.rglob("*") if p.name.lower() in names)


def parse_metadata(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """The JSRT clinical data by image id, from ``CLNDAT_EN.txt`` and ``CNNDAT_EN.TXT``.

    Both are ASCII with one image per line (old Mac line endings, a lone CR, which
    ``splitlines`` handles).

    - Nodule cases are tab-separated: file name, subtlety (1 extremely subtle .. 5 obvious),
      size (mm), age, sex, x, y (pixels on the 2048 grid), malignant or benign, location,
      diagnosis. In the distributed file the last two columns were cut at fixed widths (7
      and 12 characters) by extra tabs, as in ``r.middl<TAB>e lobe`` or
      ``pulmonary he<TAB>matoma``; ``_rejoin`` puts them back together.
    - Non-nodule cases are separated by spaces: file name, age, sex, ``non-nodule``.

    Lines that do not start with a JSRT file name are skipped. An unknown value ('?')
    becomes None.
    """
    records: dict[str, dict[str, Any]] = {}
    for path in paths:
        text = path.read_bytes().decode("latin-1")
        for number, line in enumerate(text.splitlines(), start=1):
            first = line.strip().split(maxsplit=1)
            if not first or not IMAGE_ID.match(Path(first[0]).stem.upper()):
                continue
            image_id = Path(first[0]).stem.upper()
            try:
                if image_id.startswith("JPCLN"):
                    record = _nodule_record(line.strip(" \r\n").split("\t")[1:])
                else:
                    record = _normal_record(line.split()[1:])
            except (ValueError, IndexError) as error:
                raise PrepareError(f"{path.name}, line {number}: {error}: {line!r}") from None
            records[image_id] = record
    return records


def _value(text: str) -> str | None:
    text = text.strip()
    return None if text in ("", "?") else text


def _int(text: str) -> int | None:
    value = _value(text)
    return None if value is None else int(value)


def _rejoin(first: str, second: str, width: int) -> str | None:
    """Undo a fixed-width cut: ``first`` was the first ``width`` characters of the text.

    A piece shorter than the width ended at a space that was dropped; a full-width piece
    was cut mid-word ('r.middl' + 'e lobe') or exactly at a space ('l.upper' + 'lobe',
    'inflammatory' + 'mass'); the second case is restored for the words that occur.
    """
    first, second = first.strip(), second.strip()
    if not second:
        return _value(first)
    joined = f"{first} {second}" if len(first) < width else first + second
    return re.sub(r"(?<=[a-z])(lobe|mass)\b", r" \1", joined)


def _nodule_record(fields: Sequence[str]) -> dict[str, Any]:
    if len(fields) < 9:
        raise ValueError(f"a nodule case needs 10 columns, but this line has {len(fields) + 1}")
    if len(fields) >= 11:  # the distributed file: location and diagnosis cut in two
        location = _rejoin(fields[7], fields[8], 7)
        diagnosis = _rejoin(fields[9], " ".join(fields[10:]), 12)
    else:
        location, diagnosis = _value(fields[7]), _value(" ".join(fields[8:]))
    return {
        "nodule": "present",
        "subtlety": _int(fields[0]),
        "nodule_size_mm": _int(fields[1]),
        "age": _int(fields[2]),
        "sex": _value(fields[3]),
        "nodule_x": _int(fields[4]),
        "nodule_y": _int(fields[5]),
        "malignancy": _value(fields[6]),
        "location": location,
        "diagnosis": diagnosis,
    }


def _normal_record(fields: Sequence[str]) -> dict[str, Any]:
    if len(fields) < 2:
        raise ValueError(f"a non-nodule case needs 4 columns, but this line has {len(fields) + 1}")
    return {"nodule": "absent", "age": _int(fields[0]), "sex": _value(fields[1])}


# --- The segmentations ------------------------------------------------------------------------


def structure_name(text: str) -> str:
    """'left lung', 'Left_Lung' and 'left-lung' all become 'left_lung'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@dataclass
class Observer:
    """One observer's segmentations: ``load(image_id)`` gives the five masks."""

    source: str
    images: set[str]
    load: Callable[[str], dict[str, np.ndarray]]
    folds: dict[str, int] = field(default_factory=dict)


def points_observer(root: Path) -> Observer:
    """The ``.pfs`` outline files under ``root`` (the SCR ``points`` or ``landmarks``)."""
    files = _by_image(root, (".pfs",))
    return Observer(f"SCR outlines in {root}", set(files), lambda i: outline_masks(files[i]))


def parse_pfs(path: Path) -> dict[str, list[tuple[float, float]]]:
    """The outlines of a ``.pfs`` file: ``{structure: [(x, y), ...]}``.

    A file holds blocks ``{ [Label=right lung] ... { x, y }, ... }``. The blocks labelled
    '... fixed' (the anatomical landmarks among the points) are skipped.
    """
    text = path.read_bytes().decode("latin-1")
    outlines = {}
    for block in re.split(r"\n\s*\}\s*,?\s*(?:\n|$)", text.replace("\r", "\n")):
        label = re.search(r"\[Label=([^\]]+)\]", block)
        if not label or label.group(1).strip().lower().endswith("fixed"):
            continue
        points = re.findall(r"\{\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\}", block)
        outlines[structure_name(label.group(1))] = [(float(x), float(y)) for x, y in points]
    return outlines


def fill_polygon(points: Sequence[tuple[float, float]], side: int = SIDE) -> np.ndarray:
    """A boolean ``side`` x ``side`` mask of the pixels whose centre lies inside the closed
    outline (even-odd rule). Pixel (row r, column c) has its centre at x = c, y = r."""
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    next_x, next_y = np.roll(xs, -1), np.roll(ys, -1)
    centres = np.arange(side, dtype=np.float64)
    mask = np.zeros((side, side), dtype=bool)
    top, bottom = max(0, int(np.floor(ys.min()))), min(side - 1, int(np.ceil(ys.max())))
    for row in range(top, bottom + 1):
        crossing = (ys <= row) != (next_y <= row)  # edges that cross this row's centre line
        if not crossing.any():
            continue
        x = xs[crossing] + (row - ys[crossing]) * (next_x[crossing] - xs[crossing]) / (
            next_y[crossing] - ys[crossing]
        )
        mask[row] = np.searchsorted(np.sort(x), centres, side="left") % 2 == 1
    return mask


def outline_masks(path: Path) -> dict[str, np.ndarray]:
    outlines = parse_pfs(path)
    missing = [name for name in STRUCTURES if len(outlines.get(name, ())) < 3]
    if missing:
        raise PrepareError(f"{path} has no outline of {', '.join(missing)}.")
    return {name: fill_polygon(outlines[name]) for name in STRUCTURES}


def mask_observer(root: Path) -> Observer:
    """Mask images under ``root``, in folders named after the structures."""
    files: dict[str, dict[str, Path]] = {}
    folds: dict[str, int] = {}
    for path in _image_files(root, (".gif", ".png", ".tif", ".tiff", ".bmp")):
        parts = [structure_name(part) for part in path.relative_to(root).parts[:-1]]
        structure = next((p for p in reversed(parts) if p in STRUCTURES), None)
        if structure is None:
            continue
        image_id = path.stem.upper()
        masks = files.setdefault(image_id, {})
        if structure in masks:
            raise PrepareError(
                f"Two masks of {structure} for {image_id}: {masks[structure]} and {path}. "
                "Point the option at one observer's masks only."
            )
        masks[structure] = path
        for part in parts:
            if match := re.fullmatch(r"fold_?([12])", part):
                folds[image_id] = int(match.group(1))
    if not files:
        raise PrepareError(
            f"No SCR masks were found under {root}: expected images in folders named "
            "'left lung', 'right lung', 'heart', 'left clavicle' and 'right clavicle'."
        )

    def load(image_id: str) -> dict[str, np.ndarray]:
        paths = files[image_id]
        missing = [name for name in STRUCTURES if name not in paths]
        if missing:
            raise PrepareError(f"{image_id}: no mask of {', '.join(missing)} under {root}.")
        return {name: read_mask(paths[name]) for name in STRUCTURES}

    return Observer(f"SCR masks in {root}", set(files), load, folds)


def read_mask(path: Path) -> np.ndarray:
    """A mask image as a boolean array; foreground is any non-zero pixel."""
    from PIL import Image

    with Image.open(path) as image:
        mask = np.asarray(image.convert("L")) > 0
    if mask.shape != (SIDE, SIDE):
        raise PrepareError(f"{path} is {mask.shape[1]} x {mask.shape[0]}, not {SIDE} x {SIDE}.")
    return mask


def _image_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        raise PrepareError(f"There is no folder at {root}.")
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in suffixes and IMAGE_ID.match(p.stem.upper())
    )


def _by_image(root: Path, suffixes: tuple[str, ...]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in _image_files(root, suffixes):
        image_id = path.stem.upper()
        if image_id in files:
            raise PrepareError(
                f"{image_id} is found twice under {root}: {files[image_id]} and {path}. "
                "Point the option at one observer's files only."
            )
        files[image_id] = path
    if not files:
        raise PrepareError(f"No SCR outline files (JPC*.pfs) were found under {root}.")
    return files


# --- Keys and the observer report -----------------------------------------------------------


def build_key(masks: dict[str, np.ndarray], nodule: str) -> dict[str, Any]:
    """A label in the ontology's format: the nodule answer and one RLE per structure."""
    return {
        "classifications": {"nodule": nodule},
        "masks": {name: rle.encode(masks[name]) for name in STRUCTURES},
    }


def validate_key(task: OntologyLabelTask, key: dict[str, Any], what: str) -> list[str]:
    try:
        task.validate(key)
    except LabelValidationError as error:
        return [f"{what}: {problem}" for problem in error.problems]
    return []


def observer_report(task: OntologyLabelTask, pairs: dict[str, tuple[dict, dict]]) -> dict:
    """Per mask class, the IoU between observer 1 and observer 2 over every image with
    both, from the label task's own consensus check (``check_consensus(...).detail``)."""
    ious: dict[str, list[float]] = {mask.name: [] for mask in task.ontology.masks}
    agreed = 0
    for gold, second in pairs.values():
        result = task.check_consensus([gold, second])
        agreed += result.agreed
        for name, detail in result.detail["masks"].items():
            ious[name].append(detail["min_iou"])
    return {
        "images_with_both_observers": len(pairs),
        "consensus_rule": task.consensus_rule_version,
        "ontology": task.ontology_version,
        "structures": {
            mask.name: _summary(sorted(ious[mask.name]), mask.threshold)
            for mask in task.ontology.masks
        },
        "images_where_every_field_agreed": agreed,
    }


def _summary(values: list[float], threshold: float) -> dict[str, Any]:
    if not values:
        return {"n": 0, "threshold": threshold}
    return {
        "n": len(values),
        "threshold": threshold,
        "mean": round(statistics.fmean(values), 4),
        "sd": round(statistics.stdev(values), 4) if len(values) > 1 else None,
        "median": round(statistics.median(values), 4),
        "p05": round(float(np.percentile(values, 5)), 4),
        "min": round(values[0], 4),
        "fraction_at_or_above_threshold": round(
            sum(v >= threshold for v in values) / len(values), 4
        ),
    }


def render_report(report: dict[str, Any]) -> str:
    pairs = report["images_with_both_observers"]
    if not pairs:
        return (
            "No second observer was given (--scr-points2 or --scr-masks2), so there is no\n"
            "observer report. The thresholds rest on the published agreement; see the ontology."
        )
    lines = [
        "Observer 1 (gold_key) against observer 2 (observer2_key): IoU per structure,",
        f"from {report['consensus_rule']} of {report['ontology']}, over {pairs} images.",
        "",
        f"{'structure':<16}{'n':>5}{'mean':>8}{'sd':>8}{'median':>8}{'p05':>8}{'min':>8}"
        f"{'thresh':>8}{'>= thr':>8}",
    ]
    for name, s in report["structures"].items():
        sd = "" if s["sd"] is None else f"{s['sd']:.3f}"
        lines.append(
            f"{name:<16}{s['n']:>5}{s['mean']:>8.3f}{sd:>8}{s['median']:>8.3f}"
            f"{s['p05']:>8.3f}{s['min']:>8.3f}{s['threshold']:>8.2f}"
            f"{s['fraction_at_or_above_threshold']:>8.1%}"
        )
    lines += [
        "",
        f"Images where the observers agree on the whole label: "
        f"{report['images_where_every_field_agreed']} of {pairs}. (Both keys take the nodule",
        "answer from the JSRT data, so only the masks can disagree here.)",
    ]
    return "\n".join(lines)


# --- The command ------------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    task = load_ontology(args.ontology)
    if task.ontology.mask_pixel_cap < SIDE * SIDE:
        raise PrepareError(f"{args.ontology} does not allow {SIDE} x {SIDE} masks.")
    png_dir = args.out / "png"
    media_root = (args.media_root or png_dir).resolve()

    images = find_images(args.jsrt)
    metadata_files = args.metadata or find_metadata(args.jsrt)
    if not metadata_files:
        raise PrepareError(
            f"No JSRT clinical data (CLNDAT_EN.txt, CNNDAT_EN.TXT) under {args.jsrt}; "
            "give the files with --metadata."
        )
    metadata = parse_metadata(metadata_files)
    first = points_observer(args.scr_points) if args.scr_points else mask_observer(args.scr_masks)
    second = None
    if args.scr_points2:
        second = points_observer(args.scr_points2)
    elif args.scr_masks2:
        second = mask_observer(args.scr_masks2)

    ids = sorted(images)[: args.limit] if args.limit else sorted(images)
    problems = []
    for image_id in ids:
        if image_id not in metadata:
            problems.append(f"{image_id}: not in the clinical data files.")
        if image_id not in first.images:
            problems.append(f"{image_id}: no segmentation in {first.source}.")
    if problems:
        raise PrepareError("\n".join(["The inputs do not fit together:", *problems]))

    items, pairs = [], {}
    for count, image_id in enumerate(ids, start=1):
        record = metadata[image_id]
        gold = build_key(first.load(image_id), record["nodule"])
        found = validate_key(task, gold, f"{image_id} gold_key")
        properties: dict[str, Any] = {"gold_key": gold}
        if second and image_id in second.images:
            key2 = build_key(second.load(image_id), record["nodule"])
            found += validate_key(task, key2, f"{image_id} observer2_key")
            properties["observer2_key"] = key2
            pairs[image_id] = (gold, key2)
        if found:
            raise PrepareError("\n".join(["A key does not fit the ontology:", *found]))

        raw, clipped = read_jsrt_raw(images[image_id])
        pixels = to_display(raw, SIDE, not args.no_invert, args.window, args.low_pct, args.high_pct)
        png = png_dir / f"{image_id}.png"
        digest = write_png(pixels, png)
        properties |= record
        properties |= {
            "source_file": images[image_id].name,
            "source_sha256": hashlib.sha256(images[image_id].read_bytes()).hexdigest(),
            "source_pixels_clipped": clipped,
            "gold_source": f"observer 1 of the SCR database ({first.source.split(' in ')[0]}); "
            "nodule from the JSRT clinical data",
        }
        if image_id in first.folds:
            properties["scr_fold"] = first.folds[image_id]
        items.append(
            {
                "item": image_id,
                "uri": (media_root / png.name).as_uri(),
                "content_hash": digest,
                "properties": properties,
            }
        )
        if not args.quiet and count % 25 == 0:
            print(f"  {count} of {len(ids)} images done", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "items.json").write_text(json.dumps(items, indent=1) + "\n")
    report = observer_report(task, pairs)
    report["images"] = len(items)
    report["settings"] = {
        "side": SIDE,
        "invert": not args.no_invert,
        "window": args.window,
        "low_pct": args.low_pct,
        "high_pct": args.high_pct,
        "media_root": str(media_root),
        "observer1": first.source,
        "observer2": second.source if second else None,
    }
    (args.out / "observer_report.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.out / "observer_report.txt").write_text(render_report(report) + "\n")
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python pilot/jsrt/prepare.py",
        description="Convert the JSRT images and the SCR segmentations into 1024 x 1024 "
        "PNGs, gold keys and an import file for the labelflow pilot. See "
        "pilot/jsrt/README.md.",
    )
    parser.add_argument(
        "--jsrt", type=Path, required=True, help="folder with the JSRT .IMG files (any depth)"
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        nargs="+",
        help="the JSRT clinical data files (default: CLNDAT_EN.txt and CNNDAT_EN.TXT under --jsrt)",
    )
    first = parser.add_mutually_exclusive_group(required=True)
    first.add_argument(
        "--scr-points", type=Path, help="the SCR 'points' folder of .pfs outlines (observer 1)"
    )
    first.add_argument(
        "--scr-masks", type=Path, help="SCR mask images in per-structure folders (observer 1)"
    )
    second = parser.add_mutually_exclusive_group()
    second.add_argument("--scr-points2", type=Path, help="the second observer's .pfs outlines")
    second.add_argument("--scr-masks2", type=Path, help="the second observer's mask images")
    parser.add_argument("--out", type=Path, required=True, help="output folder")
    parser.add_argument(
        "--media-root",
        type=Path,
        help="the folder the server will read the PNGs from (LABELFLOW_MEDIA_ROOT), if you "
        "will move them there; the items' uris point into it (default: OUT/png)",
    )
    parser.add_argument("--ontology", type=Path, default=DEFAULT_ONTOLOGY, help="ontology file")
    parser.add_argument(
        "--window",
        choices=("percentile", "full"),
        default="percentile",
        help="grey-level window: each image's own percentiles (default) or all 12 bits",
    )
    parser.add_argument("--low-pct", type=float, default=0.5, help="low percentile (0.5)")
    parser.add_argument("--high-pct", type=float, default=99.5, help="high percentile (99.5)")
    parser.add_argument(
        "--no-invert",
        action="store_true",
        help="keep the stored grey values (by default they are inverted, so bone is bright)",
    )
    parser.add_argument("--limit", type=int, help="only the first N images (to try it out)")
    parser.add_argument("--quiet", action="store_true", help="print only errors")
    return parser.parse_args(argv)


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
