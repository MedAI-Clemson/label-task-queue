"""Hashed random draws.

Each random decision (does this item enter this stage? does it get extra labels?) uses a
number computed from the workflow seed, the stage and the item, instead of a seeded random
stream. This makes every draw deterministic, independent of the order items arrive in, and
recomputable by anyone with the seed: ``draw(1234, "expert_review", "img_017")``.
"""

from __future__ import annotations

import hashlib
import json


def draw(seed: int, *parts: str) -> float:
    """A number in [0, 1) computed from the seed and the parts (stage, item, ...).

    The parts are encoded as a JSON array, so ``("ab", "c")`` and ``("a", "bc")`` never
    collide. The number is the first 53 bits of the SHA-256 digest divided by 2**53: a float
    holds 53 bits exactly, so the result is uniform and can never round up to 1.0 (dividing
    all 64 bits by 2**64 can).
    """
    encoded = json.dumps([seed, *parts], separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / 2**53
