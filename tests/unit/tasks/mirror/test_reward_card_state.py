import importlib

reward_card = importlib.import_module("tasks.mirror.reward_card")


class _RewardCardAuto:
    def __init__(self, require_recovery=False):
        self.model = None
        self.now = 0.0
        self.require_recovery = require_recovery
        self.selected_count = 0
        self.confirm_clicks = 0
        self.cancel_clicks = 0
        self.finished = False

    def take_screenshot(self):
        self.now += 1.0
        return object()

    def mouse_to_blank(self):
        return None

    def mouse_click(self, _x, _y):
        self.confirm_clicks += 1
        if not self.require_recovery or self.cancel_clicks:
            self.finished = True

    def find_element(self, path, **_kwargs):
        if path == "mirror/road_in_mir/legend_assets.png":
            return self.finished
        if path == "mirror/road_in_mir/acquire_ego_gift_card.png":
            return False
        if path == "mirror/get_reward_card/get_reward_card_confirm_assets.png":
            return (100, 200) if self.selected_count else None
        raise AssertionError(path)

    def click_element(self, path, **_kwargs):
        if path == "mirror/road_in_mir/ego_gift_get_confirm_assets.png":
            return False
        if path == "mirror/get_reward_card/continue_choosing_assets.png":
            self.cancel_clicks += 1
            self.selected_count = 0
            return True
        if path.startswith("mirror/get_reward_card/gain_"):
            self.selected_count += 1
            return True
        raise AssertionError(path)


def test_normal_claim_does_not_cancel_selection_before_confirmation(monkeypatch):
    fake_auto = _RewardCardAuto()
    monkeypatch.setattr(reward_card, "auto", fake_auto)
    monkeypatch.setattr(reward_card, "monotonic", lambda: fake_auto.now)
    monkeypatch.setattr(reward_card, "retry", lambda: True)

    assert reward_card.get_reward_card() is True
    assert fake_auto.confirm_clicks == 1
    assert fake_auto.cancel_clicks == 0


def test_stuck_confirmation_cancels_only_after_bounded_retries(monkeypatch):
    fake_auto = _RewardCardAuto(require_recovery=True)
    monkeypatch.setattr(reward_card, "auto", fake_auto)
    monkeypatch.setattr(reward_card, "monotonic", lambda: fake_auto.now)
    monkeypatch.setattr(reward_card, "retry", lambda: True)

    assert reward_card.get_reward_card() is True
    assert fake_auto.confirm_clicks == reward_card._CLAIM_RESET_AFTER + 1
    assert fake_auto.cancel_clicks == 1
