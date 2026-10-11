"""Who may claim which slot.

Used by ``request_work`` to pick a slot and by ``explain`` to say why a slot is waiting, so
the two can never disagree.

To claim a slot, a person must:

1. hold one of the stage's roles at the moment of the claim (the roles named by the config
   version in force, so an ``assign_to`` edit applies at the next claim);
2. if the slot is reserved (``labels_per_item: all``), be the person it is reserved for;
3. not have labeled this item anywhere in this workflow (no self-review; nobody labels the
   same item twice in a workflow);
4. hold no other claim on this item;
5. not have released this item in this stage before.

Rule 5 is ours, not the design note's: without it, a release would hand the same slot
straight back to the person who just released it, at the front of the default order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from labelflow.engine.queries import and_list, held_roles
from labelflow.engine.tables import Row, Tables


@dataclass(frozen=True)
class Person:
    """What eligibility needs to know about one person in one workflow, read once."""

    user: str
    roles: frozenset[str]
    labeled: Mapping[str, str]
    """Item -> the stage where this person labeled it."""
    claims: Mapping[str, Row]
    """Item -> the slot this person has claimed on it."""
    released: frozenset[tuple[str, str]]
    """(stage, item) pairs this person released before."""


def load_person(tables: Tables, workflow: str, user: str) -> Person:
    return Person(
        user=user,
        roles=frozenset(held_roles(tables, user)),
        labeled={
            r["item"]: r["stage"] for r in tables.select("labels", workflow=workflow, user=user)
        },
        claims={
            r["item"]: r
            for r in tables.select("slots", workflow=workflow, status="claimed", claimed_by=user)
        },
        released={
            (r["stage"], r["item"]) for r in tables.select("releases", workflow=workflow, user=user)
        },
    )


def why_not(person: Person, slot: Row, stage_roles: Sequence[str]) -> str | None:
    """Why the person may not claim this slot, in plain language; None when they may."""
    user, item = person.user, slot["item"]
    if slot["reserved_for"] not in (None, user):
        return f"the slot is reserved for {slot['reserved_for']}"
    if not person.roles & set(stage_roles):
        return f"{user} does not hold role {and_list(stage_roles, 'or')}"
    if item in person.labeled:
        return f"{user} already labeled this item in {person.labeled[item]}"
    claim = person.claims.get(item)
    if claim is not None and claim["slot"] != slot["slot"]:
        return f"{user} holds a claim on this item in {claim['stage']}"
    if (slot["stage"], item) in person.released:
        return f"{user} released this item in {slot['stage']} before"
    return None
