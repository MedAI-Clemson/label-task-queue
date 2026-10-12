"""Set up the Montgomery pilot on a running labelflow server, through its API.

    python pilot/montgomery/setup_pilot.py --server http://localhost:8000 --admin root \\
        --items out/items.json --accounts pilot/montgomery/accounts.example.csv

The steps are shared with the other kits (``pilot/common/kit_setup.py``): the project
``montgomery_pilot`` and its roles, the accounts, a gold set of 12 images stratified on
``tb_findings`` and the main set of the other 126, and both workflows of
``pilot/montgomery/workflows``. Passwords are generated here, never read from the
repository, and appended to ``pilot/montgomery/out/credentials.csv`` (git-ignored, readable
by you only). Safe to run again.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

import kit_setup
from kit_setup import Api, SetupError, read_accounts
from kits import MONTGOMERY as KIT

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
