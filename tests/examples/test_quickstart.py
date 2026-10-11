"""The runnable examples run, are deterministic, and print what the README says they print."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = ROOT / "examples" / "quickstart.py"


def run(script: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


@pytest.fixture(scope="module")
def output() -> str:
    return run(QUICKSTART)


def test_the_quickstart_is_deterministic(output):
    assert run(QUICKSTART) == output


def test_the_review_sample_part(output):
    assert "items:  12 final" in output
    assert "labels: annotate: ana 6, ben 6" in output
    assert "labels: expert_review: dr_smith 3" in output
    # the sampling report shows the drawn rate, overall and per annotator
    assert "expert_review  0.20    -                 12       3         0.25" in output


def test_explain_traces_the_reviewed_item_with_its_causes(output):
    assert "explain('img_003')" in output
    assert "item img_003  in workflow xray_main  status: final" in output
    rule = "rule: expert_review is the first listed stage that labeled it (1 label, label 5)"
    assert rule in output
    assert "expert_review.sample = 0.2 (draw 0.1374 < 0.2, admitted)" in output
    assert "annotate.assign_to = annotator and ben holds annotator" in output
    assert "final = [expert_review, annotate]" in output


def test_the_items_export(output):
    assert "img_003  final   expert_review  PA" in output
    assert "img_004  final   expert_review  AP" in output
    assert "img_001  final   annotate       PA" in output


def test_the_training_round_part(output):
    assert "items:  8 none" in output
    assert "labels: gold_read: lee 8, mia 8, noor 8" in output
    assert "gold_read  lee      8       8       8       1.00" in output
    assert "gold_read  mia      8       8       6       0.75" in output
    assert "gold_read  noor     8       8       4       0.50" in output
    assert "score_against: gold_key" in output
    assert "The project's event log holds" in output
