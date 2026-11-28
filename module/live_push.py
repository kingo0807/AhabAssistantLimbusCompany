"""把 AALC 的运行日志实时推给 MaixCAM2 板子（板子上的 aalc-live 服务）。

替代原来的 ``start_push.cmd`` + ``pc_push_log.ps1``：进程内起一个线程，跟着 AALC 启停，
不需要单独窗口、不需要计划任务，也不受 PowerShell 5.1 编码问题影响。

配置：安装目录下的 ``live_push.json``，不存在就不启动（默认关闭，避免把某台板子的地址编进发布包）。

    {"url": "http://maixcam2-9d05.local:8091", "token": "<upload.txt 里的口令>", "interval": 3}

token 省略时读安装目录 ``upload.txt``（和日志上传、远程画面接口同一个口令）。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

CONFIG_NAME = "live_push.json"
LOG_RELATIVE = Path("logs") / "debugLog.log"
TOKEN_FILE = "upload.txt"
MAX_BATCH_LINES = 400
MAX_PENDING_LINES = 4000
RETRY_BACKOFF_S = 30.0


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


class LivePush:
    def __init__(self, app_dir: Path | None = None):
        self.app_dir = Path(app_dir) if app_dir else _app_dir()
        self.cfg = _read_json(self.app_dir / CONFIG_NAME)
        self.url = str(self.cfg.get("url") or "").rstrip("/")
        self.interval = float(self.cfg.get("interval") or 3)
        self.token = str(self.cfg.get("token") or "").strip() or self._read_token()
        self.log_path = self.app_dir / LOG_RELATIVE
        self._offset = None
        self._ident = None
        self._pending: list[str] = []
        self._next_try = 0.0
        self._stop = threading.Event()

    def _read_token(self) -> str:
        try:
            return (self.app_dir / TOKEN_FILE).read_text("utf-8").strip()
        except Exception:
            return ""

    # ---------- 日志读取 ----------
    def _read_new_lines(self) -> list[str]:
        try:
            st = self.log_path.stat()
        except OSError:
            return []
        ident = (st.st_dev, st.st_ino)
        if self._ident != ident or self._offset is None or st.st_size < self._offset:
            # 首次 / 轮转 / 被截断：从末尾开始，不重放历史
            self._ident = ident
            self._offset = st.st_size
            return []
        if st.st_size == self._offset:
            return []
        try:
            with self.log_path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(1024 * 1024)
                self._offset = f.tell()
        except OSError:
            return []
        text = chunk.decode("utf-8", "replace")
        return [ln for ln in text.splitlines() if ln.strip()]

    # ---------- HTTP ----------
    def _post(self, path: str, body: bytes) -> bool:
        u = urlparse(self.url)
        if not u.hostname:
            return False
        secure = u.scheme == "https"
        port = u.port or (443 if secure else 80)
        headers = {"Content-Type": "text/plain; charset=utf-8",
                   "X-AALC-Token": self.token,
                   "Content-Length": str(len(body))}
        conn = None
        try:
            conn = (http.client.HTTPSConnection(u.hostname, port, timeout=6)
                    if secure else http.client.HTTPConnection(u.hostname, port, timeout=6))
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            return 200 <= resp.status < 300
        except Exception:
            return False
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    # ---------- 主循环 ----------
    def tick(self) -> tuple[bool, bool]:
        """跑一轮，返回 (心跳是否成功, 推送是否成功或有待推内容)。"""
        now = time.monotonic()
        hb = self._post("/ping", b"{}")
        if not hb:
            self._next_try = now + RETRY_BACKOFF_S
            return False, False

        self._pending.extend(self._read_new_lines())
        if len(self._pending) > MAX_PENDING_LINES:
            del self._pending[: len(self._pending) - MAX_PENDING_LINES]
        if not self._pending or now < self._next_try:
            return True, bool(self._pending)

        batch = self._pending[:MAX_BATCH_LINES]
        body = ("\n".join(batch)).encode("utf-8")
        if self._post("/push", body):
            del self._pending[: len(batch)]
            self._next_try = 0.0
            return True, True
        self._next_try = now + RETRY_BACKOFF_S
        return True, False

    def run(self) -> None:
        from module.logger import log

        while not self._stop.is_set():
            try:
                ok, _ = self.tick()
                if not ok and not self._stop.wait(self.interval):
                    continue
            except Exception as exc:  # 任何异常都不能影响主程序
                log.debug(f"日志推送异常: {type(exc).__name__}: {exc}")
            self._stop.wait(self.interval)


def start_live_push(app_dir: Path | None = None) -> threading.Thread | None:
    """启动日志推送线程；没有 live_push.json 或没有 token 就跳过。"""
    from module.logger import log

    pusher = LivePush(app_dir)
    if not pusher.url:
        return None
    if not pusher.token:
        log.warning("日志实时推送未启动：没有 token（live_push.json 或 upload.txt）")
        return None
    thread = threading.Thread(target=pusher.run, name="LiveLogPush", daemon=True)
    thread.start()
    log.info(f"日志实时推送已启动：{pusher.url}（每 {pusher.interval:g} 秒）")
    return thread
