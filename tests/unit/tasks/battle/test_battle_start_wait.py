import importlib

battle_module = importlib.import_module("tasks.battle.battle")


def test_battle_start_wait_uses_fast_polling(monkeypatch) -> None:
    calls = []

    class _Auto:
        def wait_for_element(self, target, **kwargs):
            calls.append((target, kwargs))
            return (10, 20)

    monkeypatch.setattr(battle_module, "auto", _Auto())

    assert battle_module.Battle._wait_for_battle_animation_start(1.0) is True
    assert calls == [
        (
            "battle/pause_assets.png",
            {"timeout": 1.0, "poll_interval": 0.15, "threshold": 0.75},
        )
    ]
