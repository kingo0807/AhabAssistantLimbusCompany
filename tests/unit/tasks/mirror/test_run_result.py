import importlib

mirror_module = importlib.import_module("tasks.mirror.mirror")


def _handler():
    handler = object.__new__(mirror_module.Mirror)
    handler.floor_times = [None] * 5
    handler.floor = 0
    handler.resumed_run = False
    return handler


def test_only_new_or_configured_partial_runs_count_as_completed():
    result = mirror_module.MirrorRunResult

    assert result.COMPLETED.counts_as_run is True
    assert result.INTENTIONAL_PARTIAL.counts_as_run is True
    assert result.RESUMED_COMPLETED.counts_as_run is False
    assert result.DEFEATED.counts_as_run is False
    assert result.CLAIMED_PREVIOUS_REWARD.counts_as_run is False
    assert result.FAILED.counts_as_run is False


def test_floor_timer_uses_monotonic_start_and_never_invents_missing_duration(monkeypatch):
    time_logs = []
    monkeypatch.setattr(mirror_module, "to_log_with_time", lambda message, elapsed: time_logs.append((message, elapsed)))
    handler = _handler()

    handler._record_floor_pack(0, now=100.0)
    handler._record_floor_pack(1, now=125.0)

    assert handler.floor_times[:2] == [100.0, 125.0]
    assert time_logs == [("启动后第1层卡包", 25.0)]
    assert handler.resumed_run is False


def test_missing_previous_floor_marks_resumed_without_logging_absurd_time(monkeypatch):
    time_logs = []
    monkeypatch.setattr(mirror_module, "to_log_with_time", lambda message, elapsed: time_logs.append((message, elapsed)))
    handler = _handler()

    handler._record_floor_pack(3, now=500.0)
    handler.floor = 4
    handler._log_last_floor_time(now=530.0)

    assert handler.resumed_run is True
    assert time_logs == [("启动后第4层卡包", 30.0)]


def test_pass_coin_parser_recovers_common_ocr_substitutions():
    parse = mirror_module.Mirror._parse_pass_coins

    assert parse(("x8",)) == 8
    assert parse(("OXB",)) == 8
    assert parse(("xB",)) == 8
    assert parse(None) is None


def test_search_road_replans_locally_before_expensive_fallback(monkeypatch):
    class _Auto:
        def mouse_to_blank(self):
            return None

    class _Map:
        def __init__(self):
            self.floor_map = ["stale"]
            self.steps = iter(["U", "D"])
            self.entered = []

        def get_next_step(self):
            return next(self.steps)

        def enter_next_node(self, direction):
            self.entered.append(direction)
            return direction == "D"

    handler = _handler()
    handler.mirror_map = _Map()
    monkeypatch.setattr(mirror_module, "auto", _Auto())
    monkeypatch.setattr(mirror_module.cfg, "mirror_keyboard_simple_pathfinding", False)
    monkeypatch.setattr(
        mirror_module,
        "search_road_default_distance",
        lambda: (_ for _ in ()).throw(AssertionError("局部重规划成功后不应进入最近节点兜底")),
    )

    assert handler.search_road() is True
    assert handler.mirror_map.entered == ["U", "D"]
    assert handler.mirror_map.floor_map == []


class _FloorMap:
    def __init__(self):
        self.refreshed = []

    def refresh_floor(self, floor):
        self.refreshed.append(floor)


class _FloorAuto:
    def __init__(self, *, settings_opened=True, settings_ready=True, remaining_floors=3):
        self.settings_opened = settings_opened
        self.settings_ready = settings_ready
        self.settings_is_open = False
        self.remaining_floors = remaining_floors
        self.closed_at = None

    def click_element(self, path, **_kwargs):
        assert path == "mirror/road_in_mir/setting_assets.png"
        if self.settings_opened:
            self.settings_is_open = True
        return self.settings_opened

    def wait_for_element(self, path, **_kwargs):
        assert path == "mirror/road_in_mir/to_window_assets.png"
        return (1000, 500) if self.settings_is_open and self.settings_ready else False

    def find_element(self, path, **_kwargs):
        if path == "mirror/road_in_mir/to_window_assets.png":
            return (1000, 500) if self.settings_is_open and self.settings_ready else False
        if path == "mirror/road_in_mir/not_passed_floor.png":
            return [(index, 0) for index in range(self.remaining_floors)]
        raise AssertionError(path)

    def mouse_action_with_pos(self, position):
        self.closed_at = position
        return True


def test_floor_recognition_success_refreshes_once_and_unlocks_pathfinding(monkeypatch):
    handler = _handler()
    handler.floor = 1
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    fake_auto = _FloorAuto(remaining_floors=3)
    monkeypatch.setattr(mirror_module, "auto", fake_auto)
    monkeypatch.setattr(mirror_module.cfg, "set_win_size", 1440)

    assert handler.get_which_floor() is True
    assert handler.floor == 2
    assert handler.get_floor_num is False
    assert handler.mirror_map.refreshed == [2]
    assert fake_auto.closed_at == (800, 500)


def test_floor_recognition_failure_preserves_route_cache_and_retry_flag(monkeypatch):
    handler = _handler()
    handler.floor = 1
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    monkeypatch.setattr(mirror_module, "auto", _FloorAuto(settings_ready=False))

    assert handler.get_which_floor() is False
    assert handler.floor == 1
    assert handler.get_floor_num is True
    assert handler.mirror_map.refreshed == []


def test_floor_recognition_reuses_settings_page_that_finished_loading_late(monkeypatch):
    handler = _handler()
    handler.floor = 1
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    fake_auto = _FloorAuto(settings_ready=False, remaining_floors=3)
    monkeypatch.setattr(mirror_module, "auto", fake_auto)
    monkeypatch.setattr(mirror_module.cfg, "set_win_size", 1440)

    assert handler.get_which_floor() is False
    fake_auto.settings_ready = True
    fake_auto.settings_opened = False  # 弹窗打开后底层的设置按钮已不可点击

    assert handler.get_which_floor() is True
    assert handler.floor == 2
    assert handler.mirror_map.refreshed == [2]


def test_floor_recognition_rejects_transient_multi_floor_jump(monkeypatch):
    handler = _handler()
    handler.floor = 1
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    monkeypatch.setattr(mirror_module, "auto", _FloorAuto(remaining_floors=0))
    monkeypatch.setattr(mirror_module.cfg, "set_win_size", 1440)

    assert handler.get_which_floor() is False
    assert handler.floor == 1
    assert handler.get_floor_num is True
    assert handler.mirror_map.refreshed == []


def test_floor_recognition_uses_sequential_floor_after_two_empty_template_reads(monkeypatch):
    handler = _handler()
    handler.floor = 1
    handler.floor_times[0] = 100.0
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    fake_auto = _FloorAuto(remaining_floors=0)
    monkeypatch.setattr(mirror_module, "auto", fake_auto)
    monkeypatch.setattr(mirror_module.cfg, "set_win_size", 1440)

    assert handler.get_which_floor() is False
    assert handler.get_which_floor() is True
    assert handler.floor == 2
    assert handler.get_floor_num is False
    assert handler.mirror_map.refreshed == [2]


def test_resumed_floor_five_rejects_zero_match_without_independent_evidence(monkeypatch):
    handler = _handler()
    handler.floor = 0
    handler.get_floor_num = True
    handler.mirror_map = _FloorMap()
    monkeypatch.setattr(mirror_module, "auto", _FloorAuto(remaining_floors=0))
    monkeypatch.setattr(mirror_module.cfg, "set_win_size", 1440)

    assert handler.get_which_floor() is False
    assert handler.floor == 0
    assert handler.mirror_map.refreshed == []
