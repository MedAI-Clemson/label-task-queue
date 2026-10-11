"""Generate the fixtures the prototype page loads: synthetic X-ray-like images, the ontology
as JSON, and a fixture "work" response (slot + WorkPacket + shown) with one prior label.

Run with the project venv (needs numpy, pyyaml and labelflow; no Pillow):

    python spikes/mask-tools/fixtures/make_fixtures.py [--big]

--big also writes a 2048 x 2048 image and packet for the performance notes (not committed).
The prior label's masks are encoded with labelflow.labeltask.rle in the compressed form, so
the page's JavaScript decoder is exercised against the project's own encoder.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import yaml

from labelflow.labeltask import rle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ONTOLOGY = ROOT / "examples" / "ontologies" / "chest_xray.yaml"


def write_png_gray(path: Path, pixels: np.ndarray) -> None:
    """Write an 8-bit grayscale PNG with the standard library only."""
    height, width = pixels.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in pixels.astype(np.uint8))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def synthetic_xray(size: int, seed: int = 7) -> np.ndarray:
    """A smooth, vaguely thoracic picture: bright body, two darker lung ellipses, mild noise."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float64) / size
    body = 150 * np.exp(-(((x - 0.5) / 0.35) ** 2 + ((y - 0.55) / 0.5) ** 2))
    lungs = np.zeros_like(body)
    for cx in (0.34, 0.66):
        lungs += 70 * np.exp(-((((x - cx) / 0.13) ** 2 + ((y - 0.5) / 0.26) ** 2) ** 2))
    spine = 40 * np.exp(-(((x - 0.5) / 0.04) ** 2))
    ribs = 12 * np.sin(y * 60) * np.exp(-(((x - 0.5) / 0.3) ** 2))
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 3, (size, size))
    picture = 30 + body - lungs + spine + ribs + noise
    return np.clip(picture, 0, 255).round().astype(np.uint8)


def lung_mask(size: int) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64) / size
    inside = np.zeros((size, size), dtype=bool)
    for cx in (0.34, 0.66):
        inside |= ((x - cx) / 0.12) ** 2 + ((y - 0.5) / 0.24) ** 2 <= 1
    return inside


def effusion_mask(size: int) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64) / size
    return (((x - 0.68) / 0.08) ** 2 + ((y - 0.7) / 0.05) ** 2) <= 1


def write_fixtures(size: int, suffix: str) -> None:
    image_path = HERE / f"xray_{size}.png"
    write_png_gray(image_path, synthetic_xray(size))
    content_hash = "sha256:" + hashlib.sha256(image_path.read_bytes()).hexdigest()

    prior_label = {
        "classifications": {"view": "PA", "quality": "good"},
        "masks": {
            "lung": rle.encode(lung_mask(size)),
            "effusion": rle.encode(effusion_mask(size)),
        },
    }
    work = {
        "slot": {
            "slot": 17,
            "workflow": "review_sample",
            "stage": "review",
            "item": "img-0042",
            "index": 1,
            "status": "claimed",
            "reserved_for": None,
            "claimed_by": "reviewer-1",
            "claimed_at": datetime.now(UTC).isoformat(),
            "label": None,
            "config_version": 1,
        },
        "packet": {
            "workflow": "review_sample",
            "slot": 17,
            "stage": "review",
            "item": "img-0042",
            "uri": image_path.name,
            "content_hash": content_hash,
            "show_prior": True,
            "prior_labels": [{"label": 101, "stage": "label", "value": prior_label}],
            "consensus": {
                "stage": "label",
                "label_ids": [101, 102],
                "agreed": False,
                "rule_version": "ontology-consensus@1",
                "detail": {
                    "classifications": {
                        "view": {"agreed": True, "values": ["PA", "PA"]},
                        "quality": {"agreed": False, "values": ["good", "poor"]},
                    },
                    "masks": {
                        "lung": {
                            "agreed": True,
                            "threshold": 0.8,
                            "min_iou": 0.91,
                            "pairwise_iou": [[0, 1, 0.91]],
                            "sizes": [[size, size], [size, size]],
                        },
                        "effusion": {
                            "agreed": False,
                            "threshold": 0.5,
                            "min_iou": 0.31,
                            "pairwise_iou": [[0, 1, 0.31]],
                            "sizes": [[size, size], [size, size]],
                        },
                    },
                },
            },
        },
        "shown": {
            "content_hash": content_hash,
            "prior_label_ids": [101],
            "show_prior": True,
            "consensus_rule_version": "ontology-consensus@1",
        },
    }
    (HERE / f"work{suffix}.json").write_text(json.dumps(work, indent=1))
    print(f"wrote {image_path.name} ({image_path.stat().st_size} bytes) and work{suffix}.json")


def main() -> None:
    ontology = yaml.safe_load(ONTOLOGY.read_text())
    (HERE / "ontology.json").write_text(json.dumps(ontology, indent=1))
    write_fixtures(512, "")
    if "--big" in sys.argv:
        write_fixtures(2048, "_2048")


if __name__ == "__main__":
    main()
