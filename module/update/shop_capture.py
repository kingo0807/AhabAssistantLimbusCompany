"""运行时截图采集：把商店/队伍界面存成 PNG，随日志一起上传。

用途只有一个——让远程排查/标定拿到真实游戏截图。除此之外不参与任何逻辑。

约束（按重要性）：
1. **绝不影响主流程**：任何异常都吞掉，最坏情况就是不截图。
2. **不能撑爆上传**：日志包总大小有上限，所以每类只留最近几张。
3. **可关**：安装目录放一个 no_capture.txt 就整个关闭，不用改配置/UI。

目录：logs/captures/shop_L<层>_<时间>.png、logs/captures/team_<队号>_<时间>.png
"""

import os
import re
import time

from module.logger import log

CAPTURE_DIR = os.path.join("logs", "captures")
KEEP_SHOP = 3           # 商店截图本地最多留几张
KEEP_TEAM = 2           # 队伍截图本地最多留几张
MAX_WIDTH = 0           # 0 = 保留原始分辨率（标定/OCR 都要原图）
SAVE_FORMAT = "PNG"     # 无损保存，不做有损压缩
SWITCH_FILE = "no_capture.txt"

SHOP_PREFIX = "shop_"
TEAM_PREFIX = "team_"


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
        return _save(image, f"shop_L{layer}", SHOP_PREFIX, KEEP_SHOP)
    except Exception as e:
        log.debug(f"截图采集失败（已忽略）: {type(e).__name__}: {e}")
        return None


def capture_team(team_no: int, remark: str = "") -> str | None:
    """镜牢选完队伍时调一次，返回保存路径（失败或关闭时返回 None）。

    队伍截图只用来核对「这一局用的是哪队、队里是什么人格」，所以文件名里
    带上队号和备注（备注压成 ASCII），方便和 records/mirror_runs.csv 对上。
    """
    if not _enabled():
        return None
    try:
        from module.automation.screenshot import ScreenShot

        image = ScreenShot.take_screenshot(gray=False)
        if image is None:
            return None
        suffix = re.sub(r"[^0-9A-Za-z]+", "", remark or "")[:12]
        tag = f"team_{team_no}" + (f"_{suffix}" if suffix else "")
        return _save(image, tag, TEAM_PREFIX, KEEP_TEAM)
    except Exception as e:
        log.debug(f"队伍截图失败（已忽略）: {type(e).__name__}: {e}")
        return None


def _save(image, tag: str, prefix: str, keep: int) -> str | None:
    try:
        if MAX_WIDTH and image.width > MAX_WIDTH:
            ratio = MAX_WIDTH / float(image.width)
            new_size = (MAX_WIDTH, max(1, int(image.height * ratio)))
            image = image.resize(new_size)
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        name = f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.{SAVE_FORMAT.lower()}"
        path = os.path.join(CAPTURE_DIR, name)
        image.convert("RGB").save(path, SAVE_FORMAT, optimize=True)
        _prune(prefix, keep)
        log.debug(f"已采集截图 {path} ({os.path.getsize(path)/1024/1024:.1f}MB {image.width}x{image.height})")
        return path
    except Exception as e:
        log.debug(f"保存截图失败（已忽略）: {type(e).__name__}: {e}")
        return None


def _prune(prefix: str, keep: int) -> None:
    """同一类只保留最新的 keep 张，避免 logs 目录越跑越大。

    按前缀分开剪，商店截图多了不会把队伍截图挤掉。
    """
    try:
        files = [
            os.path.join(CAPTURE_DIR, n)
            for n in os.listdir(CAPTURE_DIR)
            if n.lower().endswith((".png", ".jpg")) and n.startswith(prefix)
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def latest_captures(limit: int = 1, prefix: str = SHOP_PREFIX) -> list[tuple[str, str]]:
    """返回 [(绝对路径, zip 内文件名), ...]，最新的在前。prefix 用来区分商店/队伍。"""
    try:
        if not os.path.isdir(CAPTURE_DIR):
            return []
        files = [
            os.path.join(CAPTURE_DIR, n)
            for n in os.listdir(CAPTURE_DIR)
            if n.lower().endswith((".png", ".jpg")) and n.startswith(prefix)
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return [(p, os.path.basename(p)) for p in files[:limit]]
    except OSError:
        return []
