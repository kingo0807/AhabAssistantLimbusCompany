"""战斗待机界面识别的轻量状态。"""

from dataclasses import dataclass


@dataclass
class BattleRecognitionState:
    """区分整场统计和连续失败，避免一次降级后整场持续使用重型 OCR。"""

    total_failures: int = 0
    consecutive_failures: int = 0
    last_turn_ocr_at: float = float("-inf")

    def record_failure(self) -> None:
        self.total_failures += 1
        self.consecutive_failures += 1

    def record_operation(self) -> None:
        self.consecutive_failures = 0

    def should_try_turn_ocr(
        self,
        now: float,
        *,
        prefer_ocr: bool = False,
        force: bool = False,
        failure_threshold: int = 10,
        min_interval: float = 0.75,
    ) -> bool:
        """仅在需要降级识别且距离上次 OCR 足够久时允许执行。"""

        if not (force or prefer_ocr or self.consecutive_failures >= failure_threshold):
            return False
        if now - self.last_turn_ocr_at < min_interval:
            return False
        self.last_turn_ocr_at = now
        return True
