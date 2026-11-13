"""运行时截图采集：把商店界面存成 JPEG，随日志一起上传。

用途只有一个——让远程排查/标定拿到真实游戏截图。除此之外不参与任何逻辑。

约束（按重要性）：
1. **绝不影响主流程**：任何异常都吞掉，最坏情况就是不截图。
2. **不能撑爆上传**：隧道上行实测 20~27 KB/s，所以只保留最近几张、上传时只带一张。
3. **可关**：安装目录放一个 no_capture.txt 就整个关闭，不用改配置/UI。

目录：logs/captures/shop_L<层>_<时间>.jpg
"""

import os
import time

from module.logger import log

CAPTURE_DIR = os.path.join("logs", "captures")
KEEP = 3                # 本地最多留几张
MAX_WIDTH = 1440        # 超过就等比缩小（标定用的是比例，不受影响）
JPEG_QUALITY = 78       # 实测 1440 宽 q78 约 150~250 KB
MAX_FILE_BYTES = 300 * 1024   # 单张上限，超了就降质量重存，别把上传预算一个人吃掉
SWITCH_FILE = "no_capture.txt"


def _enabled() -> bool:
    try:
        return not os.path.exists(SWITCH_FILE)
    except OSError:
        return True


def capture_shop(layer: int = 0) -> str | None:
    """进商店时调一次，返回保存路径（失败或关闭时返回 None）。"""
    if not _enabled():
        return None
    try:
        # 延迟导入：截图模块会连带拉起 pywin32/pyautogui，
        # 放在这里就不会在 import 阶段影响别的模块。
        from module.automation.screenshot import ScreenShot

        image = ScreenShot.take_screenshot(gray=False)
        if image is None:
            return None
        return _save(image, f"shop_L{layer}")
    except Exception as e:
        log.debug(f"截图采集失败（已忽略）: {type(e).__name__}: {e}")
        return None


def _save(image, tag: str) -> str | None:
    try:
        if image.width > MAX_WIDTH:
            ratio = MAX_WIDTH / float(image.width)
            new_size = (MAX_WIDTH, max(1, int(image.height * ratio)))
            image = image.resize(new_size)
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        name = f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        path = os.path.join(CAPTURE_DIR, name)
        rgb = image.convert("RGB")
        quality = JPEG_QUALITY
        while True:
            rgb.save(path, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(path) <= MAX_FILE_BYTES or quality <= 55:
                break
            quality -= 8
        _prune()
        log.debug(f"已采集截图 {path} ({os.path.getsize(path)/1024:.0f}KB q{quality})")
        return path
    except Exception as e:
        log.debug(f"保存截图失败（已忽略）: {type(e).__name__}: {e}")
        return None


def _prune() -> None:
    """只保留最新的 KEEP 张，避免 logs 目录越跑越大。"""
    try:
        files = [
            os.path.join(CAPTURE_DIR, n)
            for n in os.listdir(CAPTURE_DIR)
            if n.lower().endswith(".jpg")
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def latest_captures(limit: int = 1) -> list[tuple[str, str]]:
    """返回 [(绝对路径, zip 内文件名), ...]，最新的在前。"""
    try:
        if not os.path.isdir(CAPTURE_DIR):
            return []
        files = [
            os.path.join(CAPTURE_DIR, n)
            for n in os.listdir(CAPTURE_DIR)
            if n.lower().endswith(".jpg")
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return [(p, os.path.basename(p)) for p in files[:limit]]
    except OSError:
        return []
