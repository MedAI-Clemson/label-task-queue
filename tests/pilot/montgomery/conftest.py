"""Fixtures for the Montgomery pilot kit: synthetic data in the real Montgomery formats.

Nothing here is real patient data. ``fake_montgomery`` writes four radiographs laid out as
in ``NLM-MontgomeryCXRSet.zip``:

- ``CXR_png/MCUCXR_*.png``: 8-bit greyscale PNGs at the real sizes, 4020 x 4892 (portrait)
  and 4892 x 4020 (landscape);
- ``ManualMask/leftMask`` and ``ManualMask/rightMask``: the lungs as 1-bit PNGs of the same
  size, the folders named by IMAGE side (``leftMask`` holds the patient's right lung);
- ``ClinicalReadings/MCUCXR_*.txt``: sex, age and the reading, with LF or CRLF lines, an
  unknown sex 'O' and a "same pt as" note;
- the Mac zip's leftovers: ``__MACOSX/._*``, ``.DS_Store`` and ``Thumbs.db``.

Each lung is a rectangle, and the image is bright exactly inside the two rectangles, so
the alignment of the shrunk image and masks can be checked. Writing the full-size images
takes a few seconds, once per session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

PORTRAIT, LANDSCAPE = (4020, 4892), (4892, 4020)  # (width, height)

# Per source: size, and per lung (the PATIENT's) its rectangle as first row, last row, first
# column, last column (inclusive). The patient's right lung is on the left of the image.
SOURCES = {
    "MCUCXR_0001_0": (
        PORTRAIT,
        {"right_lung": (900, 3900, 600, 1800), "left_lung": (900, 3700, 2200, 3400)},
    ),
    "MCUCXR_0002_0": (
        LANDSCAPE,
        {"right_lung": (700, 3300, 1000, 2200), "left_lung": (700, 3100, 2700, 3900)},
    ),
    "MCUCXR_0104_1": (
        PORTRAIT,
        {"right_lung": (1000, 3800, 500, 1700), "left_lung": (1000, 3600, 2300, 3500)},
    ),
    "MCUCXR_0117_1": (
        LANDSCAPE,
        {"right_lung": (800, 3200, 900, 2100), "left_lung": (800, 3000, 2800, 4000)},
    ),
}
READINGS = {
    "MCUCXR_0001_0": "Patient's Sex: F \nPatient's Age: 027Y\nnormal\n",
    "MCUCXR_0002_0": "Patient's Sex: O \nPatient's Age: 005Y\nnormal\n",
    "MCUCXR_0104_1": "Patient's Sex: M \nPatient's Age: 053Y\ninactive TB scars LUL\n",
    "MCUCXR_0117_1": (
        "Patient's Sex: F \r\nPatient's Age: 085Y\r\nstable CXR since 6M; off Rx since 6M "
        "(same pt as MCUCXR_0104_1)\r\n\r\n"
    ),
}
BACKGROUND, LUNG = 40, 220


def rectangle(size: tuple[int, int], box: tuple[int, int, int, int]) -> np.ndarray:
    width, height = size
    top, bottom, left, right = box
    mask = np.zeros((height, width), dtype=bool)
    mask[top : bottom + 1, left : right + 1] = True
    return mask


@dataclass
class FakeMontgomery:
    root: Path  # the MontgomerySet folder
    ids: list[str]

    @staticmethod
    def mask(source_id: str, lung: str) -> np.ndarray:
        size, boxes = SOURCES[source_id]
        return rectangle(size, boxes[lung])

    @staticmethod
    def size(source_id: str) -> tuple[int, int]:
        return SOURCES[source_id][0]


def write_source(root: Path, source_id: str) -> None:
    from PIL import Image  # the test modules skip themselves without Pillow

    size, boxes = SOURCES[source_id]
    right, left = rectangle(size, boxes["right_lung"]), rectangle(size, boxes["left_lung"])
    pixels = np.full(right.shape, BACKGROUND, dtype=np.uint8)
    pixels[right | left] = LUNG
    Image.fromarray(pixels, mode="L").save(root / "CXR_png" / f"{source_id}.png")
    # named by image side: leftMask is the lung on the left of the image, the patient's right
    Image.fromarray(right).save(root / "ManualMask" / "leftMask" / f"{source_id}.png")
    Image.fromarray(left).save(root / "ManualMask" / "rightMask" / f"{source_id}.png")
    (root / "ClinicalReadings" / f"{source_id}.txt").write_bytes(
        READINGS[source_id].encode("ascii")
    )


@pytest.fixture(scope="session")
def fake_montgomery(tmp_path_factory: pytest.TempPathFactory) -> FakeMontgomery:
    top = tmp_path_factory.mktemp("montgomery_download")
    root = top / "MontgomerySet"
    for folder in ("CXR_png", "ManualMask/leftMask", "ManualMask/rightMask", "ClinicalReadings"):
        (root / folder).mkdir(parents=True)
    for source_id in SOURCES:
        write_source(root, source_id)
    # what the Mac zip tool left in the real download
    (root / "CXR_png" / "Thumbs.db").write_bytes(b"\0" * 64)
    (root / "ManualMask" / ".DS_Store").write_bytes(b"\0" * 64)
    mac = top / "__MACOSX" / "MontgomerySet" / "CXR_png"
    mac.mkdir(parents=True)
    (mac / "._MCUCXR_0001_0.png").write_bytes(b"\0" * 120)
    return FakeMontgomery(root=root, ids=sorted(SOURCES))


@pytest.fixture(scope="session")
def mc_prepare(load_pilot_script) -> ModuleType:
    return load_pilot_script("prepare", "montgomery")


@pytest.fixture(scope="session")
def mc_setup(load_pilot_script) -> ModuleType:
    return load_pilot_script("setup_pilot", "montgomery")


@pytest.fixture(scope="session")
def mc_export(load_pilot_script) -> ModuleType:
    return load_pilot_script("export_pilot", "montgomery")


@pytest.fixture(scope="session")
def mc_prepared(fake_montgomery: FakeMontgomery, mc_prepare, tmp_path_factory) -> Path:
    """The output folder of a full prepare run, pointed at the folder above MontgomerySet
    (as unzipped, with __MACOSX beside it)."""
    out = tmp_path_factory.mktemp("montgomery_prepared")
    argv = ["--montgomery", str(fake_montgomery.root.parent), "--out", str(out), "--quiet"]
    assert mc_prepare.main(argv) == 0
    return out
