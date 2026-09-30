"""Fixed audience quotas for zh_x_v1.

The shares are configurable assumptions about a fictional sample. They are
not a census. Every dimension is dealt out independently and sums to the
slot count exactly.
"""

from __future__ import annotations

import random

AUDIENCE_VERSION = "zh_x_v1"

# (id, Chinese label, weight). Weights are the canonical 120-person shares.
CIRCLES = (
    ("tech", "技术", 16),
    ("crypto", "币圈", 10),
    ("overseas", "海外", 8),
    ("mainland", "大陆", 14),
    ("hk_tw", "港台", 8),
    ("politics", "键政", 8),
    ("feminism", "女权", 6),
    ("antifeminism", "反女权", 6),
    ("humor", "段子", 10),
    ("marketing", "营销", 8),
    ("passerby", "路人", 18),
    ("bot", "机器人", 8),
)
RELATIONS = (
    ("stranger", "陌生", 72),
    ("mutual", "互关", 18),
    ("fan", "粉丝", 18),
    ("following", "关注", 12),
)
ACTIVITIES = (
    ("low", "低", 60),
    ("mid", "中", 48),
    ("high", "高", 12),
)
LANGUAGES = (
    ("zh_hans", "简体", 90),
    ("zh_hant", "繁体", 18),
    ("mixed", "中英夹杂", 12),
)
INFLUENCE = (
    ("low", "低", 78),
    ("mid", "中", 30),
    ("high", "高", 12),
)
STANCES = (
    ("supportive", "倾向支持", 24),
    ("opposing", "倾向反对", 18),
    ("neutral", "中性", 48),
    ("unexpressed", "未表态", 30),
)

DIMENSIONS = (
    ("circle", CIRCLES),
    ("relation", RELATIONS),
    ("activity", ACTIVITIES),
    ("language", LANGUAGES),
    ("influence", INFLUENCE),
    ("prior_stance", STANCES),
)


def allocate(total: int, spec: tuple[tuple[str, str, int], ...]) -> dict[str, int]:
    """Largest-remainder allocation. The values sum to ``total``."""

    if total < 0:
        raise ValueError("total must be non-negative")
    weight_sum = sum(weight for _key, _label, weight in spec)
    raw = [(key, total * weight / weight_sum) for key, _label, weight in spec]
    counts = {key: int(value) for key, value in raw}
    remainder = total - sum(counts.values())
    ranked = sorted(raw, key=lambda item: (-(item[1] - int(item[1])), item[0]))
    for key, _value in ranked[:remainder]:
        counts[key] += 1
    return counts


def quota_table(agent_count: int) -> dict[str, dict[str, int]]:
    return {name: allocate(agent_count, spec) for name, spec in DIMENSIONS}


def _labels(spec: tuple[tuple[str, str, int], ...]) -> dict[str, str]:
    return {key: label for key, label, _weight in spec}


def build_slots(agent_count: int, seed: int) -> list[dict]:
    """Assign immutable public labels. Slot ids are ``a001`` … in order."""

    if agent_count < 1 or agent_count > 240:
        raise ValueError("人数必须在 1 到 240 之间。")
    columns: dict[str, list[str]] = {}
    for name, spec in DIMENSIONS:
        counts = allocate(agent_count, spec)
        deck: list[str] = []
        for key, _label, _weight in spec:
            deck.extend([key] * counts[key])
        random.Random(f"{seed}:{name}").shuffle(deck)
        columns[name] = deck
    label_maps = {name: _labels(spec) for name, spec in DIMENSIONS}
    slots = []
    for index in range(agent_count):
        slot = {"agent_id": f"a{index + 1:03d}"}
        for name, _spec in DIMENSIONS:
            key = columns[name][index]
            slot[name] = key
            slot[f"{name}_label"] = label_maps[name][key]
        slots.append(slot)
    return slots


def wave_sizes(agent_count: int, round_count: int) -> list[int]:
    """Split people into non-overlapping waves. Each person is in one wave."""

    if round_count < 1 or round_count > 4:
        raise ValueError("波次必须在 1 到 4 之间。")
    base, extra = divmod(agent_count, round_count)
    return [base + (1 if index < extra else 0) for index in range(round_count)]


def split_waves(slots: list[dict], round_count: int) -> list[list[dict]]:
    sizes = wave_sizes(len(slots), round_count)
    waves: list[list[dict]] = []
    cursor = 0
    for size in sizes:
        waves.append(slots[cursor:cursor + size])
        cursor += size
    return waves
