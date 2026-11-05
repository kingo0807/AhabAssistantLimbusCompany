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
import zipfile
from datetime import datetime

import requests

from module.logger import log
from module.update.check_update import update_manifest_url

MAX_BYTES = 15 * 1024 * 1024


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
    """把 logs 目录里的日志打成 zip；新文件优先，总大小超限就跳过大的。"""
    buf = io.BytesIO()
    entries = []
    if os.path.isdir("./logs"):
        for name in os.listdir("./logs"):
            path = os.path.join("./logs", name)
            if os.path.isfile(path) and name.lower().endswith((".log", ".txt")):
                entries.append((os.path.getmtime(path), path, name))

    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, path, name in sorted(entries, reverse=True):
            with open(path, "rb") as f:
                data = f.read()
            if total + len(data) > max_bytes:
                continue
            zf.writestr("logs/" + name, data)
            total += len(data)
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
    name += "-" + os.environ.get("COMPUTERNAME", "pc") + ".zip"

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
        log.error(f"上传日志失败:{e}")
        return False, f"上传失败:{e}"
