from tasks.mirror.theme_pack_profile import get_theme_pack_target_floor, select_theme_pack_index

PROFILE = {
    "minimum_samples": 3,
    "minimum_improvement_seconds": 15,
    "floors": {
        "2": {
            "当斩": {"samples": 6, "average_seconds": 227},
            "地狱鸡": {"samples": 5, "average_seconds": 285},
        }
    },
}


def test_current_floor_maps_to_next_theme_pack_floor() -> None:
    assert get_theme_pack_target_floor(0) == 1
    assert get_theme_pack_target_floor(1) == 2
    assert get_theme_pack_target_floor(4) == 5


def test_invalid_current_floor_disables_learned_profile() -> None:
    assert get_theme_pack_target_floor(None) is None
    assert get_theme_pack_target_floor(-1) is None
    assert get_theme_pack_target_floor(5) is None
    assert get_theme_pack_target_floor(True) is None


def test_second_floor_selection_uses_second_floor_profile() -> None:
    target_floor = get_theme_pack_target_floor(1)
    index, reason = select_theme_pack_index(["地狱鸡", "当斩"], [1, 0], target_floor, 0, PROFILE)

    assert index == 1
    assert reason == "历史楼层耗时择优"


def test_low_weight_safety_exclusion_is_never_overridden() -> None:
    index, reason = select_theme_pack_index(["地狱鸡", "当斩"], [1, -1], 2, 0, PROFILE)

    assert index == 0
    assert reason is None


def test_small_or_unproven_difference_keeps_original_order() -> None:
    close_profile = {
        **PROFILE,
        "floors": {
            "2": {
                "甲": {"samples": 5, "average_seconds": 240},
                "乙": {"samples": 5, "average_seconds": 230},
            }
        },
    }

    assert select_theme_pack_index(["甲", "乙"], [0, 0], 2, 0, close_profile) == (0, None)
    assert select_theme_pack_index(["甲", "乙"], [0, 0], 1, 0, close_profile) == (0, None)


def test_malformed_profile_never_breaks_original_selection() -> None:
    malformed = {
        "minimum_samples": "bad",
        "floors": {"2": {"当斩": {"samples": 6}}},
    }

    assert select_theme_pack_index(["地狱鸡", "当斩"], [1, 0], 2, 0, malformed) == (0, None)
