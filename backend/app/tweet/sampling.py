"""Rule candidates. Silence never becomes its own model call.

The base prior is none 90%, like 7%, reply 1.5%, repost 0.8%, quote 0.7%.
Activity multipliers rescale the non-none weights and then renormalize.
The 10,000-draw frequency check uses the unweighted prior. Activity is a
separate check.
"""

from __future__ import annotations

import random

BASE_WEIGHTS = (
    ("none", 9000),
    ("like", 700),
    ("reply", 150),
    ("repost", 80),
    ("quote", 70),
)
ACTIVITY_MULTIPLIER = {
    "low": 0.55,
    "mid": 1.0,
    "high": 1.8,
}
MODEL_ACTIONS = ("like", "reply", "repost", "quote")


def action_weights(activity: str = "mid") -> list[tuple[str, float]]:
    multiplier = ACTIVITY_MULTIPLIER[activity]
    scaled = [
        (name, weight if name == "none" else weight * multiplier)
        for name, weight in BASE_WEIGHTS
    ]
    total = sum(weight for _name, weight in scaled)
    return [(name, weight / total) for name, weight in scaled]


def target_frequencies(activity: str = "mid") -> dict[str, float]:
    return dict(action_weights(activity))


def sample_action(rng: random.Random, activity: str = "mid") -> str:
    pick = rng.random()
    cursor = 0.0
    weights = action_weights(activity)
    for index, (name, weight) in enumerate(weights):
        cursor += weight
        if pick < cursor or index == len(weights) - 1:
            return name
    return "none"


def draw_candidates(slots: list[dict], seed: int) -> dict[str, str]:
    """One candidate per slot, in slot order, from a fixed seed."""

    rng = random.Random(f"candidates:{seed}")
    return {
        slot["agent_id"]: sample_action(rng, slot.get("activity") or "mid")
        for slot in slots
    }


def model_items(slots: list[dict], candidates: dict[str, str]) -> list[dict]:
    """Non-none candidates only. Rule silence stays out of the batch."""

    return [slot for slot in slots if candidates[slot["agent_id"]] != "none"]
