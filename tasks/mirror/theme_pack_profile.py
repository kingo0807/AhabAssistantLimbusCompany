"""用实测楼层耗时为同档主题包提供保守的二级排序。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

PROFILE_PATH = Path("./assets/config/theme_pack_floor_profile.json")


def get_theme_pack_target_floor(current_floor: int | None, max_floor: int = 5) -> int | None:
    """把已完成楼层换算为主题包将要进入的目标楼层。"""

    if type(current_floor) is not int or not 0 <= current_floor < max_floor:
        return None
    return current_floor + 1


@lru_cache(maxsize=1)
def load_theme_pack_floor_profile(path: str | Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            profile = json.load(file)
    except (OSError, ValueError, TypeError):
        return {}
    return profile if isinstance(profile, dict) else {}


def select_theme_pack_index(
    pack_names: Sequence[str],
    weights: Sequence[int],
    floor: int | None,
    preferred_threshold: int,
    profile: dict[str, Any] | None = None,
) -> tuple[int, str | None]:
    """保持原权重为主，只在相邻权重档内用足量样本选择明显更快的卡包。"""

    if not weights or len(pack_names) != len(weights):
        raise ValueError("主题包名称与权重列表必须非空且长度一致")

    default_index = max(range(len(weights)), key=weights.__getitem__)
    if floor is None:
        return default_index, None

    data = profile if profile is not None else load_theme_pack_floor_profile()
    floor_profiles = data.get("floors", {}).get(str(floor), {})
    if not isinstance(floor_profiles, dict):
        return default_index, None

    try:
        minimum_samples = max(1, int(data.get("minimum_samples", 3)))
        minimum_improvement = max(0.0, float(data.get("minimum_improvement_seconds", 15)))
    except (TypeError, ValueError):
        return default_index, None
    max_weight = weights[default_index]
    candidates: list[tuple[int, float]] = []
    for index, (name, weight) in enumerate(zip(pack_names, weights)):
        # -1 以内视作同一可接受档，低于优选阈值的危险包仍不参与学习排序。
        if weight < preferred_threshold or weight < max_weight - 1:
            continue
        stats = floor_profiles.get(name)
        if not isinstance(stats, dict):
            continue
        try:
            sample_count = int(stats.get("samples", 0))
            average_seconds = float(stats["average_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if sample_count < minimum_samples or average_seconds <= 0:
            continue
        candidates.append((index, average_seconds))

    if not candidates:
        return default_index, None

    learned_index, learned_seconds = min(candidates, key=lambda item: item[1])
    default_stats = floor_profiles.get(pack_names[default_index])
    if isinstance(default_stats, dict):
        try:
            default_samples = int(default_stats.get("samples", 0))
            default_seconds = float(default_stats["average_seconds"])
        except (KeyError, TypeError, ValueError):
            default_samples = 0
            default_seconds = 0.0
        if default_samples >= minimum_samples and default_seconds - learned_seconds < minimum_improvement:
            return default_index, None

    if learned_index == default_index:
        return default_index, None
    return learned_index, "历史楼层耗时择优"
