"""Set up the JSRT pilot on a running labelflow server, through its API.

    python pilot/jsrt/setup_pilot.py --server http://localhost:8000 --admin root \\
        --items out/items.json [--accounts people.csv]

The steps are shared with the other kits (``pilot/common/kit_setup.py``): the project
``jsrt_pilot`` and its roles, the accounts, a gold set of 20 images stratified on
``nodule`` and the main set of the rest, and both workflows of ``pilot/jsrt/workflows``.
Generated passwords go to ``pilot/jsrt/out/credentials.csv``. Safe to run again.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

import kit_setup
from kit_setup import Api, SetupError, read_accounts
from kits import JSRT as KIT

split_items = partial(kit_setup.split_items, stratify_by=KIT.stratify_by)

__all__ = ["KIT", "Api", "SetupError", "main", "read_accounts", "split_items"]


def main(
    argv: Sequence[str] | None = None,
    password: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    return kit_setup.main(KIT, argv, password, say)


if __name__ == "__main__":
    sys.exit(main())
