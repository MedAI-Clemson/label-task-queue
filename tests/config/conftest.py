"""Shared helpers for the config tests."""

import difflib
import os
from pathlib import Path

import pytest


@pytest.fixture
def assert_snapshot():
    """Check text against a snapshot file. Set UPDATE_SNAPSHOTS=1 to rewrite the file."""

    def check(path: Path, actual: str) -> None:
        if os.environ.get("UPDATE_SNAPSHOTS"):
            path.write_text(actual)
            return
        assert path.exists(), (
            f"There is no snapshot {path.name} yet. Create it with:\n"
            f"    UPDATE_SNAPSHOTS=1 pytest {path.parent}"
        )
        expected = path.read_text()
        if actual != expected:
            diff = "\n".join(
                difflib.unified_diff(
                    expected.splitlines(),
                    actual.splitlines(),
                    "snapshot",
                    "actual",
                    lineterm="",
                )
            )
            pytest.fail(
                f"{path.name} does not match. If the change is intended, update the "
                f"snapshots with:\n    UPDATE_SNAPSHOTS=1 pytest {path.parent}\n\n{diff}"
            )

    return check
