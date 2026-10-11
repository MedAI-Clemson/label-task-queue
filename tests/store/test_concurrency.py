"""Claimants racing for one open slot: the database lets exactly one of them win."""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from ltq.engine import Engine, EngineError, verify_chain
from ltq.labeltask import LabelTaskRegistry
from ltq.labeltask.standin import StandInLabelTask
from ltq.store import SqlStore, connect, create_schema

ONE_SLOT = {
    "workflow": "wf",
    "dataset": "ds",
    "label_task": "standin@1",
    "stages": [{"name": "annotate", "assign_to": "annotator"}],
}
ROUNDS = 8


def claim_by_asking(engine: Engine, user: str):
    return engine.request_work("wf", user)


def claim_by_choosing(engine: Engine, user: str):
    return engine.claim("wf", 1, user)  # slot 1 is the only slot there is


@pytest.mark.parametrize("claim", [claim_by_asking, claim_by_choosing], ids=["ask", "choose"])
@pytest.mark.parametrize("shared_store", [False, True], ids=["own-store", "shared-store"])
def test_two_claimants_race_for_one_slot_and_exactly_one_wins(
    tmp_path, build_engine, claim: Callable, shared_store: bool
):
    for round_number in range(ROUNDS):
        url = f"sqlite:///{tmp_path / f'race{round_number}.db'}"
        engines = [connect(url, synchronous="OFF") for _ in range(3)]
        try:
            create_schema(engines[0])
            build_engine(SqlStore(engines[0], "demo"), items=1, config=ONE_SLOT)
            if shared_store:
                stores = [SqlStore(engines[1], "demo")] * 2
            else:  # each claimant has its own connection pool, as separate processes would
                stores = [SqlStore(engines[1], "demo"), SqlStore(engines[2], "demo")]
            results = race(stores, ("ana", "ben"), claim)
        finally:
            for engine in engines:
                engine.dispose()
        # The winner holds a Slot; the loser was told there is nothing (None) or refused.
        winners = [user for user, outcome in results.items() if hasattr(outcome, "slot")]
        assert len(winners) == 1, results
        assert all(
            o is None or isinstance(o, EngineError) for u, o in results.items() if u not in winners
        )
        verify(url, winners)


def race(stores: list[SqlStore], users: tuple[str, ...], claim: Callable) -> dict:
    """Each user claims in a thread of their own, released together by a barrier."""
    barrier = threading.Barrier(len(users))
    results: dict = {}

    def claimant(store: SqlStore, user: str) -> None:
        engine = Engine(store, LabelTaskRegistry([StandInLabelTask()]))
        store.select("roles")  # open the connection before the race starts
        barrier.wait(timeout=10)
        try:
            results[user] = claim(engine, user)
        except EngineError as refused:  # "slot is not open"
            results[user] = refused

    threads = [
        threading.Thread(target=claimant, args=pair) for pair in zip(stores, users, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a claimant is stuck"
    return results


def verify(url: str, winners: list[str]) -> None:
    engine = connect(url)
    try:
        store = SqlStore(engine, "demo")
        claims = [e for e in store.events() if e.type == "slot_claimed"]
        assert [c.user for c in claims] == winners
        assert [s["claimed_by"] for s in store.select("slots", status="claimed")] == winners
        assert verify_chain(store.events()) == len(store.events())
        assert store.verify_derived().identical
    finally:
        engine.dispose()
