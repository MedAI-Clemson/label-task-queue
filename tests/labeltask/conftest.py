"""Shared fixtures for the label task tests: the example ontology and handmade masks."""

from pathlib import Path

import numpy as np
import pytest

from labelflow.labeltask import OntologyLabelTask, load_ontology, rle

EXAMPLE_ONTOLOGIES = Path(__file__).resolve().parents[2] / "examples" / "ontologies"


@pytest.fixture
def example_dir() -> Path:
    return EXAMPLE_ONTOLOGIES


@pytest.fixture(scope="session")
def task() -> OntologyLabelTask:
    """The example chest X-ray ontology (masks: lung at 0.8, effusion at 0.5).

    Session-scoped (a label task is never changed by use), so Hypothesis tests can take it.
    """
    return load_ontology(EXAMPLE_ONTOLOGIES / "chest_xray.yaml")


@pytest.fixture
def block():
    """``block(n)`` is a 10 x 10 mask whose first n pixels (reading row by row) are foreground.

    Two blocks always overlap fully, so their IoU is easy to read off: ``block(80)`` against
    ``block(100)`` is 80 / 100 = 0.8.
    """

    def make(n: int, height: int = 10, width: int = 10) -> np.ndarray:
        pixels = np.zeros(height * width, dtype=bool)
        pixels[:n] = True
        return pixels.reshape(height, width)

    return make


@pytest.fixture
def make_label(block):
    """Build a label for the example ontology.

    ``lung`` and ``effusion`` are boolean arrays (or ready-made RLE dicts). By default the
    lung mask is all 100 pixels and the effusion mask is empty. A ``quality`` of None leaves
    the optional classification out.
    """

    def make(view="PA", quality="good", lung=None, effusion=None) -> dict:
        lung = block(100) if lung is None else lung
        effusion = block(0) if effusion is None else effusion
        classifications = {"view": view}
        if quality is not None:
            classifications["quality"] = quality
        return {
            "classifications": classifications,
            "masks": {
                "lung": lung if isinstance(lung, dict) else rle.encode(lung),
                "effusion": effusion if isinstance(effusion, dict) else rle.encode(effusion),
            },
        }

    return make
