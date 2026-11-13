"""把本地运行日志打包上传到自建服务，方便远程排查问题。

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

# 2026-11-13 起日志上传直接落 NanoKVM Go 本地（局域网），不再走 Cloudflare 隧道，
# 所以上限从 1MB 放宽到 8MB：截图改成原图 PNG 后单张就有 2~4MB。
# 设备端 aalc-serve.py 的 MAX_BYTES 同为 8MB，两边要对齐。
MAX_BYTES = 8 * 1024 * 1024


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


def collect_logs(max_bytes: int = MAX_BYTES, note: str = "") -> bytes:
    """把**最新**那份日志的尾部打成 zip，附带一张最新的商店截图。

    以前是把 logs 里所有文件都塞进去，结果一个大日志就有 4 MB，
    在这条隧道上必然 524。现在只取最近修改的那一个文件，并且只取尾部
    max_bytes 字节（debugLog.log 里最新的内容在末尾）。

    截图（logs/captures/*.jpg）按剩余预算附带；JPEG 压缩率低，所以只带一张，
    而且日志已经吃掉大半预算时直接不带，保证整包还在上限内。
    """
    buf = io.BytesIO()
    newest = None
    if os.path.isdir("./logs"):
        candidates = []
        for name in os.listdir("./logs"):
            path = os.path.join("./logs", name)
            if os.path.isfile(path) and name.lower().endswith((".log", ".txt")):
                candidates.append((os.path.getmtime(path), path, name))
        if candidates:
            newest = max(candidates, key=lambda item: item[0])

    log_blob = b""
    log_name = ""
    if newest is not None:
        _, path, log_name = newest
        # 日志最多吃 55% 预算。以前这里直接用 max_bytes，日志一满就把截图额度吃成负数，
        # 表现为「截图采集成功但永远传不上去」。
        log_budget = max(64 * 1024, int(max_bytes * 0.55))
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - log_budget))
            log_blob = f.read()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if log_name:
            zf.writestr("logs/" + log_name, log_blob)
        if os.path.exists("./assets/config/version.txt"):
            with open("./assets/config/version.txt", "r", encoding="utf-8") as f:
                zf.writestr("version.txt", f.read())
        if note.strip():
            zf.writestr("note.txt", note)

        # 剩余预算给截图（日志是文本、压缩率高；截图基本按原大小占位，按未压缩算最保守）
        try:
            from module.update.shop_capture import latest_captures

            remaining = max_bytes - len(log_blob) - 12 * 1024
            for path, name in latest_captures(limit=1):
                size = os.path.getsize(path)
                if size > remaining:
                    log.debug(f"截图 {name} 有 {size} 字节，超出上传预算，跳过")
                    continue
                with open(path, "rb") as f:
                    zf.writestr("captures/" + name, f.read())
                remaining -= size
        except Exception as e:
            log.debug(f"附带截图失败（已忽略）: {e}")
    return buf.getvalue()


def upload_logs(note: str = "", timeout: int = 0) -> tuple[bool, str]:
    """打包并上传日志，返回 (是否成功, 提示文本)。调用方应放在后台线程里跑。

    timeout=0 表示按包大小自动算：隧道上行实测 10~27 KB/s，晚高峰更低，
    固定 30 秒对「日志 + 截图」这种包不够用（实测报 Read timed out）。
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
        # 按 6 KB/s 保守估，30 秒起步、最多 150 秒（Cloudflare 源站超时约 100 秒，留一点余量）
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
        # 把状态码带出来：524 是 Cloudflare 等源站超时，和 403（口令错）完全不同
        status = getattr(getattr(e, "response", None), "status_code", None)
        prefix = f"HTTP {status} " if status else ""
        hint = "（上传超时，可稍后重试）" if status == 524 else ""
        log.error(f"上传日志失败:{prefix}{e}")
        return False, f"上传失败:{prefix}{e}{hint}"
