"""Simulated labelers: run a workflow to completion on the real engine.

This is the start of dry runs. Each simulated labeler repeatedly asks for work and submits
a label for whatever it claims. Its label is the item's true value with probability
``accuracy``, else one of the other values, chosen with a seeded random generator, so two
labelers agree more often the more accurate they are. Labels have the stand-in label task's
shape, ``{"value": ...}``.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from labelflow.engine.engine import Engine


@dataclass(frozen=True)
class SimulatedLabeler:
    user: str
    accuracy: float = 1.0
    """Probability of submitting the item's true value."""


def simulate(
    engine: Engine,
    workflow: str,
    labelers: Sequence[SimulatedLabeler],
    truth: Callable[[str], str],
    values: Sequence[str],
    rng: random.Random,
    max_rounds: int = 100_000,
) -> int:
    """Let the labelers work until none of them is offered anything; return labels submitted.

    In each round the labelers ask for work in a random order. ``truth(item)`` is the
    item's true value; a wrong label is drawn from the other ``values``.
    """
    submitted = 0
    for _ in range(max_rounds):
        worked = False
        for labeler in rng.sample(list(labelers), len(labelers)):
            slot = engine.request_work(workflow, labeler.user)
            if slot is None:
                continue
            packet = engine.work_packet(workflow, slot.slot, labeler.user)
            true_value = truth(slot.item)
            value = true_value
            if rng.random() >= labeler.accuracy:
                value = rng.choice([v for v in values if v != true_value])
            engine.submit(
                workflow,
                slot.slot,
                labeler.user,
                {"value": value},
                packet.shown(),
                duration_ms=rng.randint(5_000, 120_000),
            )
            submitted += 1
            worked = True
        if not worked:
            return submitted
    raise RuntimeError(f"The simulation did not finish within {max_rounds} rounds.")
