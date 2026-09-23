from tasks.battle.recognition_state import BattleRecognitionState


def test_operation_resets_only_consecutive_failures() -> None:
    state = BattleRecognitionState()
    for _ in range(10):
        state.record_failure()

    assert state.should_try_turn_ocr(1.0) is True
    state.record_operation()

    assert state.total_failures == 10
    assert state.consecutive_failures == 0
    assert state.should_try_turn_ocr(2.0) is False


def test_turn_ocr_is_rate_limited_even_when_preferred() -> None:
    state = BattleRecognitionState()

    assert state.should_try_turn_ocr(10.0, prefer_ocr=True) is True
    assert state.should_try_turn_ocr(10.5, prefer_ocr=True) is False
    assert state.should_try_turn_ocr(10.75, prefer_ocr=True) is True
