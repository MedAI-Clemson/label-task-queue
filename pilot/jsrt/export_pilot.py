"""Download everything the JSRT pilot's audit needs from a running labelflow server.

    python pilot/jsrt/export_pilot.py --server http://localhost:8000 --admin root --out exports/

What it writes is described in ``pilot/common/kit_export.py`` and ``pilot/jsrt/README.md``.
Nothing on the server is changed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

import kit_export
from kits import JSRT as KIT


def main(
    argv: Sequence[str] | None = None,
    password: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    return kit_export.main(KIT, argv, password, say)


if __name__ == "__main__":
    sys.exit(main())
