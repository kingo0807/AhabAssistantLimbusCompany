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
