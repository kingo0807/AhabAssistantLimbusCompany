import importlib

team_formation = importlib.import_module("tasks.teams.team_formation")


class _TeamAuto:
    def __init__(self, visible_texts):
        self.visible_texts = visible_texts
        self.clicks = []
        self.selected_positions = []
        self.swipes = []

    def take_screenshot(self):
        return object()

    def find_element(self, path, **_kwargs):
        if path == "home/first_prompt_assets.png":
            return False
        if path == "teams/identify_assets.png":
            return (2300, 100)
        raise AssertionError(path)

    def click_element(self, path, **_kwargs):
        raise AssertionError(path)

    def mouse_click(self, x, y):
        self.clicks.append((x, y))

    def mouse_action_with_pos(self, position, **_kwargs):
        self.selected_positions.append(position)
        return True

    def mouse_swipe_for_scroll(self, x, y, **kwargs):
        self.swipes.append((x, y, kwargs))

    def get_text_positions_from_screenshot(self, _crop):
        return self.visible_texts


def _configure_ordered_selection(monkeypatch, fake_auto):
    monkeypatch.setattr(team_formation, "auto", fake_auto)
    monkeypatch.setattr(team_formation, "sleep", lambda _seconds: None)
    monkeypatch.setattr(team_formation.cfg, "set_win_size", 1440)
    monkeypatch.setattr(team_formation.cfg, "simulator", False)
    monkeypatch.setattr(team_formation.cfg, "select_team_by_order", True)


def test_visible_ordered_team_match_is_exact(monkeypatch):
    fake_auto = _TeamAuto({"TEAMS #10": [100, 200], "编队#1": [100, 300]})
    monkeypatch.setattr(team_formation, "auto", fake_auto)

    assert team_formation._find_visible_ordered_team(1, (0, 0, 500, 700)) == [100, 300]


def test_ordered_selection_uses_verified_visible_team_without_reset(monkeypatch):
    fake_auto = _TeamAuto({"TEAMS #3": [100, 300]})
    _configure_ordered_selection(monkeypatch, fake_auto)

    assert team_formation.select_battle_team(3) is True
    assert fake_auto.selected_positions == [[100, 300]]
    assert fake_auto.swipes == []


def test_ordered_selection_falls_back_to_original_reset_when_ocr_is_uncertain(monkeypatch):
    fake_auto = _TeamAuto({"自定义队伍": [100, 300]})
    _configure_ordered_selection(monkeypatch, fake_auto)

    assert team_formation.select_battle_team(1) is True
    assert len(fake_auto.swipes) == 3
    assert fake_auto.selected_positions == []
