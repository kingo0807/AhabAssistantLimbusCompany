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

# 隧道上行实测只有 20~27 KB/s，整包上传会撞上 Cloudflare 的等待上限（524）。
# 这里限制的是"原始未压缩字节"，压缩后通常只有 100 KB 左右，几秒就能传完。
MAX_BYTES = 1024 * 1024


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
    """把**最新**那份日志的尾部打成 zip。

    以前是把 logs 里所有文件都塞进去，结果一个大日志就有 4 MB，
    在这条隧道上必然 524。现在只取最近修改的那一个文件，并且只取尾部
    max_bytes 字节（debugLog.log 里最新的内容在末尾）。
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

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if newest is not None:
            _, path, name = newest
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                zf.writestr("logs/" + name, f.read())
        if os.path.exists("./assets/config/version.txt"):
            with open("./assets/config/version.txt", "r", encoding="utf-8") as f:
                zf.writestr("version.txt", f.read())
        if note.strip():
            zf.writestr("note.txt", note)
    return buf.getvalue()


def upload_logs(note: str = "", timeout: int = 30) -> tuple[bool, str]:
    """打包并上传日志，返回 (是否成功, 提示文本)。调用方应放在后台线程里跑。"""
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
