"""把本地运行日志、镜牢战绩和截图打包上传到自建服务，方便远程排查问题。

服务端约定：
  POST <清单主机>/upload
  Header X-AALC-Token: 安装目录 upload.txt 里的口令
  Header X-AALC-Name:  上报文件名
  Body: 原始 zip 字节
"""

import base64
import io
import os
import re
import zipfile
from datetime import datetime

import requests

from module.logger import log
from module.update.check_update import update_manifest_url

# 2026-11-14：一局镜牢的日志 + 商店截图 + 队伍截图 + 战绩 CSV 一起传，8MB 不够。
# 仍然直连局域网 NanoKVM Go（不走 Cloudflare 隧道），上限放宽到 12MB。
# 设备端 aalc-serve.py 的 MAX_BYTES 同为 12MB，两边必须对齐。
MAX_BYTES = 12 * 1024 * 1024
# 日志部分上限：当前文件整份 + 上一份轮转日志的尾巴（原始字节）。
# debugLog.log 按 5MB 切，跑图中途切过一次就只剩后半段，开局要从 .1 里捞；
# 文本压缩率高，8MB 原始日志进 zip 后通常不到 1MB，不会挤掉截图。
LOG_MAX_BYTES = 8 * 1024 * 1024
BACKUP_LOG_MAX_BYTES = 3 * 1024 * 1024
# 截图张数：队伍先装（张数少，是核对人格的唯一依据），商店后装
TEAM_CAPTURES = 2
SHOP_CAPTURES = 5
# 镜牢战绩 CSV：几 KB，无条件带（超长时只带尾部）
RECORD_PATH = os.path.join("records", "mirror_runs.csv")
RECORD_MAX_BYTES = 512 * 1024
# zip 收尾（中央目录等）预留
ZIP_ENVELOPE_BYTES = 8 * 1024


def _safe_host_name(text: str, fallback: str = "pc") -> str:
    """机器名可能含中文，而 HTTP 头只接受 latin-1，这里统一压成 ASCII。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text or "").strip("-._")
    return cleaned[:40] or fallback


def upload_endpoint() -> str:
    """从更新清单地址推导上传地址：保留主机，路径换成 /upload。"""
    manifest = update_manifest_url()
    parts = manifest.split("/")
    if len(parts) >= 3:
        return parts[0] + "//" + parts[2] + "/upload"
    return manifest


def read_token() -> str:
    """读取安装根目录下的 upload.txt（口令不写进仓库）。"""
    try:
        with open("./upload.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _newest_log_file() -> tuple[str, str] | None:
    """logs 目录里最近修改的那个 .log/.txt（debugLog.log 最新的内容在末尾）。"""
    if not os.path.isdir("./logs"):
        return None
    candidates = []
    for name in os.listdir("./logs"):
        path = os.path.join("./logs", name)
        if os.path.isfile(path) and name.lower().endswith((".log", ".txt")):
            candidates.append((os.path.getmtime(path), path, name))
    if not candidates:
        return None
    _, path, name = max(candidates, key=lambda item: item[0])
    return path, name


def _fit_log(data: bytes, budget: int) -> bytes:
    """超预算时保留头尾各一段：开头有 config/队伍信息，结尾是最新状态。"""
    if len(data) <= budget:
        return data
    head = min(len(data) // 2, max(16 * 1024, int(budget * 0.2)))
    tail = min(len(data) - head, max(0, budget - head))
    if tail <= 0:
        return data[:budget]
    marker = "\n[...日志过长，中间部分已省略...]\n".encode("utf-8")
    return data[:head] + marker + data[-tail:]


def _read_records() -> bytes:
    """读镜牢战绩 CSV；没有或读失败时返回空（不影响日志上传）。"""
    try:
        if not os.path.isfile(RECORD_PATH):
            return b""
        size = os.path.getsize(RECORD_PATH)
        with open(RECORD_PATH, "rb") as f:
            if size > RECORD_MAX_BYTES:
                f.seek(size - RECORD_MAX_BYTES)
                f.readline()  # 跳过被截断的半行
            return f.read()
    except OSError as e:
        log.debug(f"读取镜牢战绩失败（已忽略）: {e}")
        return b""


def _capture_candidates() -> list[tuple[str, str]]:
    """[(路径, zip 内文件名)]：队伍截图在前、商店截图在后，各自最新的优先。"""
    try:
        from module.update.shop_capture import latest_captures
    except Exception as e:
        log.debug(f"读取截图失败（已忽略）: {e}")
        return []
    items = list(latest_captures(limit=TEAM_CAPTURES, prefix="team_"))
    items.extend(latest_captures(limit=SHOP_CAPTURES, prefix="shop_"))
    return items


def collect_logs(max_bytes: int = MAX_BYTES, note: str = "") -> bytes:
    """打一个 zip：当前整份日志 + 上一份日志尾巴 + 战绩 CSV + 版本 + 备注 + 截图。

    预算顺序：日志（当前 + 上一份尾巴，最多 8MB）→ 战绩 CSV / 版本 / 备注（几 KB）
    → 队伍截图 → 商店截图。图片按顺序装到装不下为止，跳过了哪些、为什么跳过，
    都写进包内的 upload_manifest.txt，不再静默丢弃。
    """
    buf = io.BytesIO()
    manifest: list[str] = []

    newest = _newest_log_file()
    log_blob = b""
    log_name = ""
    backup_blob = b""
    backup_name = ""
    backup_size = 0
    log_budget = min(LOG_MAX_BYTES, max(64 * 1024, int(max_bytes * 0.66)))
    if newest is not None:
        path, log_name = newest
        with open(path, "rb") as f:
            raw = f.read()
        # 当前文件整份带；debugLog.log 按 5MB 轮转，8MB 日志预算装得下。
        # 不再按「一局开始」标记截：标记不一定在当前文件里，截了反而会把已完赛那局丢掉。
        log_blob = _fit_log(raw, log_budget)
        # 轮转过的上一份只带尾部：跑图中途切过文件时，开局那段在 .1 里。
        rest = log_budget - len(log_blob)
        if rest > 64 * 1024:
            backup_path = path + ".1"
            try:
                if os.path.isfile(backup_path):
                    backup_size = os.path.getsize(backup_path)
                    take = min(rest, BACKUP_LOG_MAX_BYTES, backup_size)
                    with open(backup_path, "rb") as f:
                        f.seek(backup_size - take)
                        backup_blob = f.read()
                    backup_name = log_name + ".1.tail"
            except OSError as e:
                log.debug(f"读取上一份日志失败（已忽略）: {e}")
        if backup_blob:
            manifest.append(
                f"logs/{backup_name}\t{len(backup_blob)} bytes (上一份日志 {backup_size} bytes 的尾部)"
            )
        manifest.append(f"logs/{log_name}\t{len(log_blob)} bytes (原文件 {len(raw)} bytes)")
        first_line = raw.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        manifest.append(f"日志首行\t{first_line[:160]}")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if backup_blob:
            zf.writestr("logs/" + backup_name, backup_blob)
        if log_name:
            zf.writestr("logs/" + log_name, log_blob)

        # 镜牢战绩：队伍 / 卡包 / 每层耗时，几 KB，无条件带
        records = _read_records()
        if records:
            zf.writestr("records/mirror_runs.csv", records)
            manifest.append(f"records/mirror_runs.csv\t{len(records)} bytes")

        if os.path.exists("./assets/config/version.txt"):
            with open("./assets/config/version.txt", "r", encoding="utf-8") as f:
                zf.writestr("version.txt", f.read())

        if note.strip():
            zf.writestr("note.txt", note)
            manifest.append(f"note.txt\t{len(note.encode('utf-8'))} bytes")

        # 剩余预算给截图：用 zip 已写入字节数做账，PNG 基本不可压缩，size 即占用
        used = buf.tell()
        for path, name in _capture_candidates():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if used + size + ZIP_ENVELOPE_BYTES > max_bytes:
                manifest.append(f"captures/{name}\t{size} bytes\t跳过：超出上传上限")
                log.warning(f"截图 {name} 有 {size} 字节，超出上传预算，跳过")
                continue
            with open(path, "rb") as f:
                zf.writestr("captures/" + name, f.read())
            manifest.append(f"captures/{name}\t{size} bytes")
            used = buf.tell()

        zf.writestr("upload_manifest.txt", "\n".join(manifest) + "\n")
    return buf.getvalue()


def upload_logs(note: str = "", timeout: int = 0) -> tuple[bool, str]:
    """打包并上传日志，返回 (是否成功, 提示文本)。调用方应放在后台线程里跑。

    timeout=0 表示按包大小自动算；局域网内一般几秒，给足余量即可。
    """
    token = read_token()
    if not token:
        return False, "未配置上传口令：请在程序目录放一个 upload.txt"

    try:
        payload = collect_logs(note=note)
    except Exception as e:
        log.error(f"打包日志失败:{e}")
        return False, f"打包日志失败:{e}"

    if not payload:
        return False, "没有可上传的日志"

    if timeout <= 0:
        # 按 6 KB/s 保守估，30 秒起步、最多 150 秒
        timeout = min(150, max(30, len(payload) // 6144 + 15))
    log.info(f"上传 {len(payload)/1024:.0f} KB，超时按 {timeout} 秒")

    name = datetime.now().strftime("%Y%m%d-%H%M%S")
    name += "-" + _safe_host_name(os.environ.get("COMPUTERNAME", "pc")) + ".zip"

    try:
        response = requests.post(
            upload_endpoint(),
            data=payload,
            timeout=timeout,
            headers={
                "X-AALC-Token": token,
                "X-AALC-Name": name,
                "X-AALC-Note": base64.b64encode(note.encode("utf-8")).decode("ascii"),
                "Content-Type": "application/octet-stream",
            },
        )
        response.raise_for_status()
        log.info("日志已上传:" + upload_endpoint())
        return True, "日志已上传，文件名 " + name
    except Exception as e:
        # 把状态码带出来：413 是超过设备端上限，403 是口令错，504 是隧道超时
        status = getattr(getattr(e, "response", None), "status_code", None)
        prefix = f"HTTP {status} " if status else ""
        hint = "（上传超时，可稍后重试）" if status == 504 else ""
        log.error(f"上传日志失败:{prefix}{e}")
        return False, f"上传失败:{prefix}{e}{hint}"
