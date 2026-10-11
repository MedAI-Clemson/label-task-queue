"""Snapshot tests: each bad config in bad/ must give exactly the message in the .txt file.

The messages are for researchers, so they are checked by eye when a snapshot is written:
read the .txt files after running ``UPDATE_SNAPSHOTS=1 pytest tests/config``.
"""

from pathlib import Path

import pytest

from ltq.engine.config import validate_config

BAD_DIR = Path(__file__).parent / "bad"
CASES = sorted(BAD_DIR.glob("*.yaml"))

# Words from Pydantic's own messages, which a researcher must never see.
JARGON = [
    "pydantic",
    "validation error",
    "input should",
    "[type=",
    "value_error",
    "string_type",
    "extra_forbidden",
    "model_type",
    "literal_error",
    "ctx",
    "ValidationError",
    "Traceback",
]


def test_there_are_enough_cases():
    assert len(CASES) >= 15


@pytest.mark.parametrize("case", CASES, ids=lambda path: path.stem)
def test_bad_config_message(case, assert_snapshot):
    report = validate_config(case.read_text())
    assert not report.ok
    assert report.config is None
    assert_snapshot(case.with_suffix(".txt"), report.render())


@pytest.mark.parametrize("case", CASES, ids=lambda path: path.stem)
def test_message_is_plain_language(case):
    text = validate_config(case.read_text()).render()
    for word in JARGON:
        assert word.lower() not in text.lower(), f"{case.stem}: message mentions {word!r}"


def test_every_snapshot_has_a_config():
    stems = {case.stem for case in CASES}
    assert {path.stem for path in BAD_DIR.glob("*.txt")} <= stems
