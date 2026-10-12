"""Fixtures for the pilot kits: loading their scripts, a real server for setup and export,
and, for the JSRT kit, synthetic data in the real JSRT and SCR formats (the Montgomery kit's
fake data are in ``montgomery/conftest.py``).

Nothing here is real patient data. ``fake_jsrt`` writes five images as JSRT ``.IMG`` files
(2048 x 2048, 12-bit, big-endian, no header) with the two clinical data files beside them,
written with the quirks of the distributed files (lone CR line endings; location and
diagnosis cut at fixed widths by extra tabs; the non-nodule file separated by spaces).
The first observer's segmentations are ``.pfs`` outline files as in the SCR ``points``
download, and also GIF masks in the original per-structure folders; the second observer's
are GIF masks moved a few pixels, so the observers' IoU is known and below 1.

Each structure is a rectangle with corners on half pixels, so the pixels it covers are
exact. Real sizes are used throughout; the fixture takes about a second.

Test modules cannot import from a conftest, so ``FakeData`` carries what the tests need.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN_PASSWORD = "admin-password-for-tests"

# The distributed nodule file cuts location at 7 characters and diagnosis at 12.
CLNDAT = [
    "JPCLN001.IMG\t5\t15\t53\tmale\t1634\t692\tmalignant\tl.upper\tlobe\tlung cancer\t",
    "JPCLN002.IMG\t2\t30\t?\tfemale\t420\t1100\tbenign\tr.middl\te lobe(S4)\tpulmonary he\tmatoma",
    "JPCLN003.IMG\t3\t12\t71\tmale\t800\t900\tbenign\tleft lu\tng\tinflammatory\tmass",
]
CNNDAT = ["JPCNN001.IMG 75  male     non-nodule", "JPCNN002.IMG 41  female   non-nodule"]
IDS = ["JPCLN001", "JPCLN002", "JPCLN003", "JPCNN001", "JPCNN002"]

# Each structure's rectangle on the 1024 grid: first row, last row, first column, last
# column (inclusive). The patient's right is on the left of the image.
RECTANGLES = {
    "right_lung": (200, 800, 150, 480),
    "left_lung": (200, 780, 560, 880),
    "heart": (560, 800, 420, 700),
    "right_clavicle": (170, 200, 200, 480),
    "left_clavicle": (170, 200, 560, 840),
}
SCR_FOLDERS = {
    "right_lung": "right lung",
    "left_lung": "left lung",
    "heart": "heart",
    "right_clavicle": "right clavicle",
    "left_clavicle": "left clavicle",
}
SHIFT = 6  # pixels the second observer's masks are moved to the right


def load_script(name: str, kit: str = "jsrt") -> ModuleType:
    """Import a script of a pilot kit (the folders are not packages) as running it does:
    the script adds ``pilot/common`` to the path and imports the shared modules from there.
    Each script is registered as ``<kit>_<name>``, so the kits' scripts of the same name
    do not replace each other."""
    module_name = f"{kit}_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "pilot" / kit / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def load_pilot_script() -> Callable[[str, str], ModuleType]:
    return load_script


@dataclass
class Server:
    url: str
    app: object


@contextmanager
def serve(database: Path, label_tasks: Path, media_root: Path) -> Iterator[Server]:
    """The app from ``create_app`` on a migrated database, with the admin ``root``, served by
    uvicorn in a thread, so the scripts' own HTTP code (urllib) is what runs."""
    import uvicorn
    from alembic import command
    from alembic.config import Config

    from labelflow.api import Settings, create_app

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    settings = Settings(
        database_uri=f"sqlite:///{database}",
        label_tasks=label_tasks,  # a kit's ontology folder as is
        media_root=media_root,
        sqlite_synchronous="OFF",
        password_cost=4,
    )
    app = create_app(settings)
    runtime = app.state.runtime
    runtime.ready()
    runtime.accounts.create("root", ADMIN_PASSWORD, "Root", is_admin=True)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    try:
        yield Server(f"http://127.0.0.1:{port}", app)
    finally:
        uv.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture(scope="session")
def serve_pilot() -> Callable[..., object]:
    """``serve(database, label_tasks, media_root)``: a context manager giving a ``Server``."""
    return serve


@pytest.fixture(scope="session")
def prepare_module() -> ModuleType:
    return load_script("prepare")


@pytest.fixture(scope="session")
def setup_module() -> ModuleType:
    return load_script("setup_pilot")


@pytest.fixture(scope="session")
def export_module() -> ModuleType:
    return load_script("export_pilot")


def rectangle(name: str, shift: int = 0) -> np.ndarray:
    top, bottom, left, right = RECTANGLES[name]
    mask = np.zeros((1024, 1024), dtype=bool)
    mask[top : bottom + 1, left + shift : right + shift + 1] = True
    return mask


def raw_image(index: int) -> np.ndarray:
    """A 2048 x 2048 12-bit image: a gradient whose stored values are high where the
    displayed image should be dark (the JSRT convention), so the top left is dark."""
    rows, cols = np.mgrid[:2048, :2048]
    values = 3600 - (rows + cols) * 3 // 4 + 37 * index
    return np.clip(values, 0, 4095).astype(">u2")


def pfs_text(name_masks: dict[str, tuple[int, int, int, int]]) -> str:
    """An SCR ``.pfs`` file: per structure a 'fixed' block (skipped) and the outline."""
    blocks = [";\r; Array sets\r;\r; Written on 2003.06.24 (time: 13:42:49) by test\r;\r\r"]
    for name, (top, bottom, left, right) in name_masks.items():
        label = SCR_FOLDERS[name]
        corners = [
            (left - 0.5, top - 0.5),
            (right + 0.5, top - 0.5),
            (right + 0.5, bottom + 0.5),
            (left - 0.5, bottom + 0.5),
        ]
        fixed = ",\r".join(f"  {{ {x:g}, {y:g} }}" for x, y in corners[:2])
        outline = ",\r".join(f"  {{ {x:g}, {y:g} }}" for x, y in corners)
        blocks.append(f"{{\r  [Label={label} fixed]\r  [LineDisplay=0]\r{fixed}\r}},\r\r")
        blocks.append(f"{{\r  [Label={label}]\r  [LineMode=ClosedContour]\r{outline}\r}},\r\r")
    return "".join(blocks)


@dataclass
class FakeData:
    root: Path
    jsrt: Path
    points1: Path
    masks1: Path
    masks2: Path
    ids: list[str] = field(default_factory=lambda: list(IDS))
    structures: list[str] = field(default_factory=lambda: list(RECTANGLES))

    @staticmethod
    def raw(index: int) -> np.ndarray:
        return raw_image(index)

    @staticmethod
    def mask(name: str, observer: int = 1) -> np.ndarray:
        return rectangle(name, SHIFT if observer == 2 else 0)


def write_masks(top: Path, shift: int) -> None:
    from PIL import Image  # the test modules skip themselves without Pillow

    for number, image_id in enumerate(IDS):
        fold = f"fold{1 + number % 2}"
        for name, folder in SCR_FOLDERS.items():
            path = top / fold / "masks" / folder / f"{image_id}.gif"
            path.parent.mkdir(parents=True, exist_ok=True)
            pixels = rectangle(name, shift).astype(np.uint8) * 255
            Image.fromarray(pixels, mode="L").save(path, format="GIF")


@pytest.fixture(scope="session")
def fake_jsrt(tmp_path_factory: pytest.TempPathFactory) -> FakeData:
    root = tmp_path_factory.mktemp("downloads")
    jsrt = root / "jsrt"
    (jsrt / "images").mkdir(parents=True)
    for index, image_id in enumerate(IDS):
        (jsrt / "images" / f"{image_id}.IMG").write_bytes(raw_image(index).tobytes())
    (jsrt / "CLNDAT_EN.txt").write_bytes(("\r".join(CLNDAT) + "\r").encode("ascii"))
    (jsrt / "CNNDAT_EN.TXT").write_bytes(("\r".join(CNNDAT) + "\r").encode("ascii"))

    points = root / "scr" / "points"
    points.mkdir(parents=True)
    for image_id in IDS:
        (points / f"{image_id}.pfs").write_bytes(pfs_text(RECTANGLES).encode("ascii"))
    write_masks(root / "scr_masks", shift=0)
    write_masks(root / "scr_observer2", shift=SHIFT)
    return FakeData(
        root=root,
        jsrt=jsrt,
        points1=points,
        masks1=root / "scr_masks",
        masks2=root / "scr_observer2",
    )


@pytest.fixture(scope="session")
def prepared(fake_jsrt: FakeData, prepare_module, tmp_path_factory) -> Path:
    """The output folder of a full prepare run: observer 1 from the outline files,
    observer 2 from GIF masks."""
    out = tmp_path_factory.mktemp("prepared")
    argv = ["--jsrt", str(fake_jsrt.jsrt), "--scr-points", str(fake_jsrt.points1)]
    argv += ["--scr-masks2", str(fake_jsrt.masks2), "--out", str(out), "--quiet"]
    assert prepare_module.main(argv) == 0
    return out
