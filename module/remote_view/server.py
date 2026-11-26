"""远程画面接口：让外部（MaixCAM2 板子、浏览器）直接看游戏窗口画面。

只读——不上报点击、不碰鼠标键盘，也不修改 Automation 的业务截图状态。
画面直接用脚本自己的截图通道（后台模式走 PrintWindow，窗口被遮挡也能取到）。

接口（默认 0.0.0.0:8093，可用环境变量覆盖）：

- ``GET /shot.jpg?w=960&q=70``  最近一帧 JPEG，带 ``X-Frame-Age-Ms`` / ``X-Frame-Size``
- ``GET /stream.mjpg?fps=3``    multipart MJPEG（给网页/播放器用）
- ``GET /status``               JSON：是否有帧、帧年龄、分辨率、版本
- ``GET /ping``                 轻量探活

鉴权：请求头 ``X-AALC-Token`` 或查询串 ``?token=``，值取环境变量 ``AALC_VIEW_TOKEN``，
没设就用安装目录 ``upload.txt`` 里那个上传口令（和日志上传同一个）。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8093
DEFAULT_MAX_WIDTH = 960
DEFAULT_QUALITY = 70
# 同一帧最多复用这么久，避免外部请求把 PrintWindow 打爆
FRAME_CACHE_S = 0.25

_STATE = {"jpeg": b"", "ts": 0.0, "size": (0, 0), "error": ""}
_STATE_LOCK = threading.Lock()


def _token() -> str:
    env = os.environ.get("AALC_VIEW_TOKEN", "").strip()
    if env:
        return env
    try:
        from module.update.upload_log import read_token

        return (read_token() or "").strip()
    except Exception:
        return ""


def _grab(max_width: int, quality: int) -> tuple[bytes, tuple[int, int]]:
    """取一帧彩色画面并编码成 JPEG。"""
    from PIL import Image

    from module.automation import auto

    shot = auto.take_monitor_screenshot(gray=False, max_age=0.0)
    if shot is None:
        raise RuntimeError("截图返回空（窗口最小化或截图失败）")
    if shot.mode != "RGB":
        shot = shot.convert("RGB")
    if max_width and shot.width > max_width:
        height = max(1, int(shot.height * max_width / shot.width))
        shot = shot.resize((max_width, height), Image.BILINEAR)
    buf = io.BytesIO()
    shot.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), shot.size


def _current(max_width: int, quality: int) -> tuple[bytes, tuple[int, int], int]:
    """带缓存的取帧，返 (jpeg, size, age_ms)。"""
    now = time.monotonic()
    with _STATE_LOCK:
        if _STATE["jpeg"] and now - _STATE["ts"] <= FRAME_CACHE_S:
            return _STATE["jpeg"], _STATE["size"], int((now - _STATE["ts"]) * 1000)
    jpeg, size = _grab(max_width, quality)
    with _STATE_LOCK:
        _STATE.update({"jpeg": jpeg, "ts": time.monotonic(), "size": size, "error": ""})
        return jpeg, size, 0


def _status() -> dict:
    with _STATE_LOCK:
        age = int((time.monotonic() - _STATE["ts"]) * 1000) if _STATE["ts"] else None
        size = _STATE["size"]
        error = _STATE["error"]
    version = ""
    try:
        from module.config import cfg

        version = getattr(cfg, "version", "") or ""
    except Exception:
        pass
    return {
        "ok": True,
        "version": version,
        "has_frame": bool(size and size[0]),
        "frame_age_ms": age,
        "size": list(size) if size else None,
        "error": error,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AALC-View/1.0"

    # ---------- 工具 ----------
    def _authed(self) -> bool:
        want = _token()
        if not want:
            return False
        got = (self.headers.get("X-AALC-Token") or "").strip()
        if not got:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in query.split("&"):
                if part.startswith("token="):
                    got = part[6:].strip()
                    break
        return got == want

    def _deny(self):
        body = json.dumps(
            {"ok": False, "error": "unauthorized（需要 X-AALC-Token，值见 upload.txt）"},
            ensure_ascii=False,
        ).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_int(self, name: str, default: int, low: int, high: int) -> int:
        try:
            raw = self.path.split(f"{name}=")[1].split("&")[0]
            return max(low, min(high, int(raw)))
        except Exception:
            return default

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/ping", "/status"):
            if not self._authed():
                return self._deny()
            return self._json(_status())

        if not self._authed():
            return self._deny()

        width = self._query_int("w", DEFAULT_MAX_WIDTH, 160, 3840)
        quality = self._query_int("q", DEFAULT_QUALITY, 20, 95)

        if path == "/shot.jpg":
            try:
                jpeg, size, age = _current(width, quality)
            except Exception as exc:
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Age-Ms", str(age))
            self.send_header("X-Frame-Size", f"{size[0]}x{size[1]}")
            self.end_headers()
            self.wfile.write(jpeg)
            return

        if path == "/stream.mjpg":
            fps = self._query_int("fps", 3, 1, 15)
            boundary = "aalc-frame"
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            period = 1.0 / fps
            try:
                while True:
                    t0 = time.monotonic()
                    try:
                        jpeg, _size, _age = _current(width, quality)
                    except Exception:
                        time.sleep(0.2)
                        continue
                    self.wfile.write(f"--{boundary}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    time.sleep(max(0.0, period - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        return self._json({"ok": False, "error": "not found", "paths": ["/shot.jpg", "/stream.mjpg", "/status", "/ping"]}, 404)

    def log_message(self, fmt, *args):  # 别刷日志
        return


def start_view_server(port: int | None = None, host: str = "0.0.0.0") -> threading.Thread | None:
    """在后台线程启动只读画面接口；失败只记一行日志，绝不影响主程序。"""
    from module.logger import log

    if port is None:
        try:
            port = int(os.environ.get("AALC_VIEW_PORT", DEFAULT_PORT))
        except Exception:
            port = DEFAULT_PORT
    if not _token():
        log.warning("远程画面接口未启动：没有可用的 token（设置 AALC_VIEW_TOKEN 或写 upload.txt）")
        return None
    try:
        srv = ThreadingHTTPServer((host, port), _Handler)
    except OSError as exc:
        log.warning(f"远程画面接口未启动：绑定 {host}:{port} 失败（{exc}）")
        return None
    thread = threading.Thread(target=srv.serve_forever, name="RemoteViewServer", daemon=True)
    thread.start()
    log.info(f"远程画面接口已启动：http://{host}:{port}/shot.jpg（只读，需 X-AALC-Token）")
    return thread
