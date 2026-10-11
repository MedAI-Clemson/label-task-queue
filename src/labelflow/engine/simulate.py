"""Simulated labelers: run a workflow to completion on the real engine.

This is the start of dry runs. Each simulated labeler repeatedly asks for work and submits
a label for whatever it claims. With probability ``accuracy`` the label is a correct one,
otherwise a wrong one; a **label factory** supplies either for any item, so the same
simulation runs any label task. ``standin_labels`` is the factory for the stand-in task.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from labelflow.engine.engine import Engine
from labelflow.labeltask import Label

LabelFactory = Callable[[str, bool], Label]
"""``factory(item, correct)``: a correct label for the item, or a plausible wrong one."""


class FixedClock:
    """An engine clock that starts at a set time and moves one minute per reading, so a
    simulation's timestamps are the same on every run."""

    def __init__(self, start: datetime = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)):
        self.now = start

    def __call__(self) -> datetime:
        current, self.now = self.now, self.now + timedelta(minutes=1)
        return current


@dataclass(frozen=True)
class SimulatedLabeler:
    user: str
    accuracy: float = 1.0
    """Probability of submitting a correct label."""


def simulate(
    engine: Engine,
    workflow: str,
    labelers: Sequence[SimulatedLabeler],
    labels: LabelFactory,
    rng: random.Random,
    max_rounds: int = 100_000,
) -> int:
    """Let the labelers work until none of them is offered anything; return labels submitted.

    In each round the labelers ask for work in a random order. Everything random comes from
    ``rng``, so a seeded generator makes the run reproducible.
    """
    submitted = 0
    for _ in range(max_rounds):
        worked = False
        for labeler in rng.sample(list(labelers), len(labelers)):
            slot = engine.request_work(workflow, labeler.user)
            if slot is None:
                continue
            packet = engine.work_packet(workflow, slot.slot, labeler.user)
            correct = rng.random() < labeler.accuracy
            engine.submit(
                workflow,
                slot.slot,
                labeler.user,
                labels(slot.item, correct),
                packet.shown(),
                duration_ms=rng.randint(5_000, 120_000),
            )
            submitted += 1
            worked = True
        if not worked:
            return submitted
    raise RuntimeError(f"The simulation did not finish within {max_rounds} rounds.")


def standin_labels(
    truth: Callable[[str], str], values: Sequence[str], rng: random.Random
) -> LabelFactory:
    """Labels for the stand-in task: ``{"value": truth(item)}`` when correct, else one of
    the other ``values``, chosen with ``rng``."""

    def factory(item: str, correct: bool) -> Label:
        true_value = truth(item)
        if correct:
            return {"value": true_value}
        return {"value": rng.choice([v for v in values if v != true_value])}

    return factory
