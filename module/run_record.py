"""镜牢每局每层的耗时记录。

原本 AALC 只在日志里以文本形式输出「第N层 总耗时:HH:MM:SS」和
「此次选择卡包关键词：X」，事后要靠正则去凑。这里改成每局直接追加
结构化 CSV，一次把「队伍 / 体系 / 难度 / 层 / 卡包 / 用时」对齐，
便于统计「队伍 × 卡包 × 用时」。

输出位置：程序目录下 ./records/mirror_runs.csv（与 ./logs 同级）
编码：utf-8，首次写入带 BOM，Excel 双击可直接打开不乱码。
"""

from __future__ import annotations

import csv
from pathlib import Path

from module.logger import log

RECORD_DIR = Path("./records")
RECORD_FILE = RECORD_DIR / "mirror_runs.csv"

HEADER = [
    "时间",
    "队伍#",
    "队伍备注",
    "体系",
    "难度",
    "整局秒",
    "战斗秒",
    "事件秒",
    "商店秒",
    "寻路秒",
    "层",
    "卡包",
    "层秒",
    "层计时完整",
]


def append_floor_records(
    *,
    ts: str,
    team_no: int,
    team_remark: str = "",
    system: str = "",
    difficulty: str = "",
    total_sec: float = 0.0,
    combat_sec: float = 0.0,
    event_sec: float = 0.0,
    shop_sec: float = 0.0,
    road_sec: float = 0.0,
    floors=None,
):
    """按层追加一局的记录。

    floors: [(层, 卡包, 秒, 计时完整)]，为空时直接返回，不创建文件。
    任何异常都只记日志，绝不向上抛，避免影响跑图。
    """
    floors = list(floors or [])
    if not floors:
        return None

    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        new_file = not RECORD_FILE.exists() or RECORD_FILE.stat().st_size == 0
        with RECORD_FILE.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                f.write("\ufeff")  # BOM，让 Excel 认出 UTF-8
                writer.writerow(HEADER)
            for floor, pack, sec, complete in floors:
                writer.writerow(
                    [
                        ts,
                        team_no,
                        team_remark,
                        system,
                        difficulty,
                        round(float(total_sec), 1),
                        round(float(combat_sec), 1),
                        round(float(event_sec), 1),
                        round(float(shop_sec), 1),
                        round(float(road_sec), 1),
                        floor,
                        pack,
                        round(float(sec), 1),
                        "是" if complete else "否",
                    ]
                )
        return RECORD_FILE
    except Exception as e:  # 记录是旁路功能，失败不能拖累主流程
        log.error(f"写入镜牢耗时记录失败：{e}")
        return None
