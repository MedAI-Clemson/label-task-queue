"""Hashed draws: deterministic, in [0, 1), unambiguous, and admission rises with the fraction."""

import hashlib

from hypothesis import given
from hypothesis import strategies as st

from labelflow.engine import draw

names = st.text(min_size=1, max_size=12)
seeds = st.integers(min_value=-(2**63), max_value=2**63)


@given(seeds, names, names)
def test_draws_are_deterministic_and_in_range(seed, stage, item):
    value = draw(seed, stage, item)
    assert value == draw(seed, stage, item)
    assert 0.0 <= value < 1.0


@given(seeds, names, names, st.floats(0, 1), st.floats(0, 1))
def test_admission_is_monotone_in_the_fraction(seed, stage, item, f1, f2):
    low, high = sorted((f1, f2))
    value = draw(seed, stage, item)
    if value < low:
        assert value < high


def test_parts_are_encoded_unambiguously():
    assert draw(1, "ab", "c") != draw(1, "a", "bc")
    assert draw(1, "a", "b") != draw(1, "a", "b", "labels_per_item")


def test_anyone_can_recompute_a_draw():
    """The draw is the first 53 bits of sha256 of the JSON array [seed, stage, item]."""
    digest = hashlib.sha256(b'[1234,"expert_review","img_017"]').digest()
    expected = (int.from_bytes(digest[:8], "big") >> 11) / 2**53
    assert draw(1234, "expert_review", "img_017") == expected


def test_draws_are_roughly_uniform():
    values = [draw(7, "stage", f"item_{i}") for i in range(4000)]
    assert 0.47 < sum(values) / len(values) < 0.53
    assert 0.18 < sum(v < 0.2 for v in values) / len(values) < 0.22
