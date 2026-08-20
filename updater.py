from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import psutil

UPDATER_VERSION = "1.6.0"
REPOSITORY = "kingo0807/AhabAssistantLimbusCompany"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MANIFEST_ASSET_NAME = "update-manifest.json"
LATEST_MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/{MANIFEST_ASSET_NAME}"
RELEASE_DOWNLOAD_BASE = f"https://github.com/{REPOSITORY}/releases/download"
DEFAULT_PACKAGE_ASSET = "AALC-Optimized-win64.zip"
PACKAGE_VERSION_MEMBER = "AALC/assets/config/version.txt"
ENTRYPOINT = "AALC.exe"
USER_AGENT = f"AALC-Optimized-Updater/{UPDATER_VERSION}"
PRESERVE_FILES = ("config.yaml", "theme_pack_list.yaml")
PRESERVE_GLOBS = (
    "config.yaml.*",
    "上传AALC日志*.exe",
    "AALC-Optimized*.zip",
)
PRESERVE_DIRECTORIES = (
    "config_backup",
    "logs",
    "pythonlogs",
    "theme_pack_weight",
    "issue_recordings",
    "update_temp",
)
_PRESERVE_FILE_NAMES = frozenset(name.casefold() for name in PRESERVE_FILES)
_PRESERVE_GLOB_PATTERNS = tuple(pattern.casefold() for pattern in PRESERVE_GLOBS)
_PRESERVE_DIRECTORY_NAMES = frozenset(name.casefold() for name in PRESERVE_DIRECTORIES)
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
ERROR_ELEVATION_REQUIRED = 740
SHELL_EXECUTE_SUCCESS = 32
SW_SHOWNORMAL = 1
DIRECTORY_SWITCH_RETRY_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0)
FILE_OPERATION_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)
DOWNLOAD_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
RETRYABLE_DIRECTORY_WINERRORS = {5, 32, 33}


class UpdaterError(RuntimeError):
    """更新无法安全完成。"""


class UpdateNetworkError(UpdaterError):
    """读取远端更新信息时发生网络错误。"""


_SAFE_RELEASE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_SAFE_ZIP_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.zip\Z", re.IGNORECASE)


def _validate_release_version(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_RELEASE_VERSION.fullmatch(value) is None:
        raise UpdaterError("更新清单没有安全有效的版本号")
    return value


def release_asset_url(version: str, asset: str) -> str:
    """根据已验证的清单字段构造公开 Release 资产直链。"""
    safe_version = _validate_release_version(version)
    if not isinstance(asset, str) or _SAFE_ZIP_ASSET.fullmatch(asset) is None:
        raise UpdaterError("更新清单没有安全有效的 ZIP 资产名")
    return (
        f"{RELEASE_DOWNLOAD_BASE}/"
        f"{urllib.parse.quote(safe_version, safe='')}/{urllib.parse.quote(asset, safe='')}"
    )


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int | None


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    asset: str
    sha256: str
    size: int | None
    entrypoint: str
    archive_root: str

    @classmethod
    def from_json(cls, payload: Any, release_tag: str) -> "UpdateManifest":
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise UpdaterError("更新清单格式或版本不受支持")

        release_tag = _validate_release_version(release_tag)
        version = _validate_release_version(payload.get("version"))
        asset = payload.get("asset")
        sha256 = payload.get("sha256")
        size = payload.get("size")
        entrypoint = payload.get("entrypoint", ENTRYPOINT)
        archive_root = payload.get("archive_root", "AALC")

        if version != release_tag:
            raise UpdaterError(f"更新清单版本 {version!r} 与 Release {release_tag!r} 不一致")
        if not isinstance(asset, str) or _SAFE_ZIP_ASSET.fullmatch(asset) is None:
            raise UpdaterError("更新清单没有安全有效的 ZIP 资产名")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise UpdaterError("更新清单没有有效的 SHA-256")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise UpdaterError("更新清单的 SHA-256 不是十六进制") from exc
        if size is not None and (not isinstance(size, int) or size <= 0):
            raise UpdaterError("更新清单的文件大小无效")
        if entrypoint != ENTRYPOINT:
            raise UpdaterError(f"更新入口必须是 {ENTRYPOINT}")
        if archive_root != "AALC":
            raise UpdaterError("更新压缩包必须使用 AALC 根目录")

        return cls(version, asset, sha256.lower(), size, entrypoint, archive_root)

    @classmethod
    def from_latest_json(cls, payload: Any) -> "UpdateManifest":
        """解析不依赖 GitHub API 的 latest Release 清单。"""
        release_tag = payload.get("version") if isinstance(payload, dict) else None
        return cls.from_json(payload, release_tag)


def _request(url: str, timeout: int = 30, extra_headers: dict[str, str] | None = None):
    headers = {
        "Accept": "application/vnd.github+json, application/octet-stream",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_json(url: str) -> Any:
    try:
        with _request(url) as response:
            content = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UpdateNetworkError(f"无法读取更新信息：{exc}") from exc
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdaterError(f"更新信息不是有效的 UTF-8 JSON：{exc}") from exc


def download_file(url: str, destination: Path, expected_size: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_percent = -1
    last_error: BaseException | None = None
    for attempt in range(len(DOWNLOAD_RETRY_DELAYS) + 1):
        downloaded = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={downloaded}-"} if downloaded else None
        try:
            with _request(url, timeout=60, extra_headers=headers) as response:
                status = getattr(response, "status", None) or response.getcode()
                # GitHub/镜像可能忽略 Range 并返回完整 200；此时必须从头写，
                # 否则会把完整包追加到残包后面，直到 SHA-256 校验才发现问题。
                if downloaded and status != 206:
                    downloaded = 0
                    partial.unlink(missing_ok=True)
                response_size = response.headers.get("Content-Length")
                body_size = int(response_size) if response_size and response_size.isdigit() else None
                total = expected_size or (downloaded + body_size if status == 206 and body_size else body_size)
                mode = "ab" if downloaded else "wb"
                with partial.open(mode) as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = min(100, downloaded * 100 // total)
                            if percent // 5 != last_percent // 5:
                                print(f"下载进度：{percent}%")
                                last_percent = percent
                    output.flush()
                    os.fsync(output.fileno())
            if expected_size is not None and downloaded != expected_size:
                raise UpdaterError(f"下载大小不符：应为 {expected_size}，实际为 {downloaded}")
            _with_file_retry(lambda: os.replace(partial, destination), f"更新包 {destination.name}")
            return
        except UpdaterError as exc:
            # 大小错误不可通过重试修复；保留 .part 供下一次运行续传，但不让错误包成为正式包。
            last_error = exc
            if "下载大小不符" not in str(exc) or attempt >= len(DOWNLOAD_RETRY_DELAYS):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= len(DOWNLOAD_RETRY_DELAYS):
                break
        delay = DOWNLOAD_RETRY_DELAYS[attempt]
        print(f"下载连接中断，保留已下载部分，{delay:g} 秒后从断点重试……")
        time.sleep(delay)

    message = f"下载更新包失败（已尝试 {len(DOWNLOAD_RETRY_DELAYS) + 1} 次）"
    if partial.is_file():
        message += f"；断点保留在 {partial}，可重新运行继续下载"
    raise UpdaterError(f"{message}：{last_error}") from last_error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_release(payload: Any) -> tuple[str, dict[str, ReleaseAsset]]:
    if not isinstance(payload, dict):
        raise UpdaterError("GitHub Release 响应格式无效")
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdaterError("latest Release 不能是草稿或预发布版本")
    tag = payload.get("tag_name")
    raw_assets = payload.get("assets")
    if not isinstance(tag, str) or not tag or not isinstance(raw_assets, list):
        raise UpdaterError("GitHub Release 缺少版本号或资产列表")

    assets: dict[str, ReleaseAsset] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("browser_download_url")
        size = item.get("size")
        if isinstance(name, str) and isinstance(url, str) and url.startswith("https://"):
            assets[name] = ReleaseAsset(name, url, size if isinstance(size, int) and size > 0 else None)
    return tag, assets


def _safe_archive_member(name: str, archive_root: str) -> Path:
    if not isinstance(name, str) or not name or "\0" in name:
        raise UpdaterError("更新压缩包包含空路径")
    portable = name.replace("\\", "/")
    pure_path = PurePosixPath(portable)
    parts = [part for part in pure_path.parts if part not in ("", ".")]
    if portable.startswith("/") or any(part == ".." or ":" in part for part in parts):
        raise UpdaterError(f"更新压缩包包含不安全路径：{name}")
    if not parts or parts[0].casefold() != archive_root.casefold():
        raise UpdaterError(f"更新压缩包内容必须位于 {archive_root}/ 下：{name}")
    return Path(*parts)


def extract_verified_zip(archive: Path, destination: Path, manifest: UpdateManifest) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as package:
            infos = package.infolist()
            if not infos:
                raise UpdaterError("更新压缩包为空")
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > 4 * 1024 * 1024 * 1024:
                raise UpdaterError("更新压缩包解压后超过 4 GiB，已拒绝")
            for info in infos:
                relative = _safe_archive_member(info.filename, manifest.archive_root)
                file_mode = info.external_attr >> 16
                if stat.S_ISLNK(file_mode):
                    raise UpdaterError(f"更新压缩包不允许符号链接：{info.filename}")
                target = destination / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdaterError(f"无法安全解压更新包：{exc}") from exc

    payload_root = destination / manifest.archive_root
    if not (payload_root / manifest.entrypoint).is_file():
        raise UpdaterError(f"更新包缺少入口文件 {manifest.entrypoint}")
    return payload_root


def inspect_local_archive(archive: Path) -> UpdateManifest:
    """从本地更新包读取版本；不访问网络，并以源文件摘要校验复制结果。"""
    archive = archive.resolve()
    if not archive.is_file():
        raise UpdaterError(f"找不到本地更新包：{archive}")
    if _SAFE_ZIP_ASSET.fullmatch(archive.name) is None:
        raise UpdaterError(f"更新包文件名不安全，请改名为 {DEFAULT_PACKAGE_ASSET}")

    try:
        with zipfile.ZipFile(archive) as package:
            version_info = package.getinfo(PACKAGE_VERSION_MEMBER)
            if version_info.file_size <= 0 or version_info.file_size > 256:
                raise UpdaterError("更新包内的版本文件大小异常")
            version = package.read(version_info).decode("utf-8").strip()
    except KeyError as exc:
        raise UpdaterError(f"更新包缺少版本文件 {PACKAGE_VERSION_MEMBER}") from exc
    except UnicodeDecodeError as exc:
        raise UpdaterError("更新包内的版本文件不是有效的 UTF-8") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdaterError(f"无法读取本地更新包：{exc}") from exc

    return UpdateManifest(
        version=_validate_release_version(version),
        asset=archive.name,
        sha256=sha256_file(archive),
        size=archive.stat().st_size,
        entrypoint=ENTRYPOINT,
        archive_root="AALC",
    )


def _copy_preserved_data(source: Path, destination: Path) -> None:
    for name in PRESERVE_FILES:
        item = source / name
        if item.is_file():
            shutil.copy2(item, destination / name)
    for pattern in PRESERVE_GLOBS:
        for item in source.glob(pattern):
            if item.is_file():
                shutil.copy2(item, destination / item.name)
    for name in PRESERVE_DIRECTORIES:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, destination / name, dirs_exist_ok=True)


def _is_preserved_relative(relative: Path) -> bool:
    """完整包更新时不读取、不覆盖、也不清理用户数据和本地更新工具。"""
    if not relative.parts:
        return False
    top_level = relative.parts[0]
    folded = top_level.casefold()
    if folded in _PRESERVE_DIRECTORY_NAMES:
        return True
    if len(relative.parts) != 1:
        return False
    if folded in _PRESERVE_FILE_NAMES:
        return True
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in _PRESERVE_GLOB_PATTERNS)


def _same_or_child(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _process_pid(process: psutil.Process) -> int:
    info = getattr(process, "info", {}) or {}
    return int(info.get("pid") or process.pid)


def _process_executable(process: psutil.Process) -> Path | None:
    info = getattr(process, "info", {}) or {}
    executable = info.get("exe")
    if not executable:
        executable = process.exe()
    return Path(executable) if executable else None


def _protected_worker_pids(install_dir: Path, current_pid: int) -> set[int]:
    """保护临时 worker 及其安装目录外的启动链，避免清理子进程时误杀自身。"""
    protected = {current_pid}
    try:
        ancestors = psutil.Process(current_pid).parents()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return protected
    for ancestor in ancestors:
        try:
            executable = _process_executable(ancestor)
            if executable is None or not _same_or_child(executable, install_dir):
                protected.add(ancestor.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            protected.add(ancestor.pid)
    return protected


def _collect_target_processes(install_dir: Path) -> list[psutil.Process]:
    """收集安装目录内的程序，以及由 AALC 拉起且可能继续占用目录的子进程。"""
    current_pid = os.getpid()
    protected = _protected_worker_pids(install_dir, current_pid)
    targets: dict[int, psutil.Process] = {}
    target_roots: list[psutil.Process] = []

    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            pid = _process_pid(process)
            if pid in protected:
                continue
            executable = _process_executable(process)
            if executable is None or not _same_or_child(executable, install_dir):
                continue
            targets[pid] = process
            target_roots.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            continue

    for root in target_roots:
        try:
            descendants = root.children(recursive=True)
        except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        for child in descendants:
            try:
                pid = _process_pid(child)
                if pid not in protected:
                    targets[pid] = child
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                continue
    return list(targets.values())


def _wait_target_processes(
    processes: list[psutil.Process],
    timeout: float,
) -> tuple[list[psutil.Process], list[psutil.Process]]:
    """兼容 Windows 无权打开高权限进程句柄的情况，并把它保留在 alive 中供诊断。"""
    try:
        return psutil.wait_procs(processes, timeout=timeout)
    except psutil.AccessDenied:
        gone: list[psutil.Process] = []
        alive: list[psutil.Process] = []
        for process in processes:
            try:
                (alive if process.is_running() else gone).append(process)
            except psutil.NoSuchProcess:
                gone.append(process)
            except (psutil.AccessDenied, OSError):
                alive.append(process)
        return gone, alive


def stop_target_processes(install_dir: Path) -> None:
    targets = _collect_target_processes(install_dir)
    if not targets:
        return
    print(f"正在关闭可能占用当前目录的 {len(targets)} 个进程……")
    for process in targets:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = _wait_target_processes(targets, timeout=10)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = _wait_target_processes(alive, timeout=5)
    if alive:
        names = []
        for process in alive[:6]:
            try:
                names.append(f"{process.name()} (PID {_process_pid(process)})")
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                names.append(f"PID {_process_pid(process)}")
        raise UpdaterError(f"仍有进程占用 AALC 目录，请关闭后重试：{', '.join(names)}")


def _blocking_process_summary(root: Path) -> str:
    """只读诊断目录占用者；不自动终止与 AALC 无关的编辑器或终端。"""
    blockers: list[str] = []
    current_pid = os.getpid()
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            pid = _process_pid(process)
            if pid == current_pid:
                continue
            reasons: list[str] = []
            executable = _process_executable(process)
            if executable is not None and _same_or_child(executable, root):
                reasons.append("程序位于该目录")
            try:
                cwd = Path(process.cwd())
                if _same_or_child(cwd, root):
                    reasons.append("当前工作目录位于该目录")
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                pass
            try:
                if any(_same_or_child(Path(item.path), root) for item in process.open_files()):
                    reasons.append("打开了目录内文件")
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                pass
            if reasons:
                info = getattr(process, "info", {}) or {}
                name = info.get("name") or process.name()
                blockers.append(f"{name} (PID {pid}：{'、'.join(dict.fromkeys(reasons))})")
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            continue
        if len(blockers) >= 6:
            break
    return "；".join(blockers)


def _retryable_directory_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in RETRYABLE_DIRECTORY_WINERRORS


def _rename_directory_with_retry(
    source: Path,
    destination: Path,
    *,
    blocker_root: Path | None = None,
) -> None:
    last_error: OSError | None = None
    for attempt in range(len(DIRECTORY_SWITCH_RETRY_DELAYS) + 1):
        try:
            source.rename(destination)
            return
        except OSError as exc:
            if not _retryable_directory_error(exc):
                raise
            last_error = exc
            if attempt >= len(DIRECTORY_SWITCH_RETRY_DELAYS):
                break
            delay = DIRECTORY_SWITCH_RETRY_DELAYS[attempt]
            print(f"目录仍被 Windows 占用，{delay:g} 秒后重试切换……")
            if blocker_root is not None:
                stop_target_processes(blocker_root)
            time.sleep(delay)

    details = _blocking_process_summary(blocker_root) if blocker_root is not None else ""
    suffix = f"；检测到：{details}" if details else "；请关闭打开该目录的终端、编辑器或安全软件后重试"
    raise UpdaterError(f"Windows 未能释放 AALC 目录（已重试 {len(DIRECTORY_SWITCH_RETRY_DELAYS)} 次）{suffix}") from last_error


def _retryable_file_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or getattr(exc, "winerror", None) in RETRYABLE_DIRECTORY_WINERRORS
        or getattr(exc, "errno", None) in {5, 13, 32, 33}
    )


def _with_file_retry(action, description: str) -> None:
    """参考 ALAS：Windows 短暂占用时指数退避，且只重试明确的占用错误。"""
    last_error: OSError | None = None
    for attempt in range(len(FILE_OPERATION_RETRY_DELAYS) + 1):
        try:
            action()
            return
        except OSError as exc:
            if not _retryable_file_error(exc):
                raise
            last_error = exc
            if attempt >= len(FILE_OPERATION_RETRY_DELAYS):
                break
            delay = FILE_OPERATION_RETRY_DELAYS[attempt]
            print(f"{description}仍被 Windows 短暂占用，{delay:g} 秒后重试……")
            time.sleep(delay)
    raise UpdaterError(f"{description}持续被占用，未修改更新完成标记：{last_error}") from last_error


def _remove_tree_entry(path: Path) -> None:
    def remove() -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    _with_file_retry(remove, f"路径 {path.name}")


def _copy_file_atomically(source: Path, target: Path) -> None:
    if target.exists() and target.is_dir():
        _remove_tree_entry(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.update-{uuid.uuid4().hex[:8]}")
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        shutil.copystat(source, temporary)
        _with_file_retry(lambda: os.replace(temporary, target), f"文件 {target.name}")
    finally:
        temporary.unlink(missing_ok=True)


def _mirror_tree(source: Path, destination: Path) -> None:
    """文件级事务安装；根目录和用户数据从不改名、删除或覆盖。"""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or not destination.is_dir():
        raise UpdaterError("原目录兼容安装需要两个已存在的目录")
    if _same_or_child(source, destination) or _same_or_child(destination, source):
        raise UpdaterError("拒绝在相互嵌套的目录之间执行兼容安装")

    source_files: set[Path] = set()
    source_directories: set[Path] = {Path()}
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        kept_directories: list[str] = []
        for name in directories:
            item = root_path / name
            relative = relative_root / name
            if _is_preserved_relative(relative):
                continue
            if item.is_symlink():
                raise UpdaterError(f"更新内容不允许目录链接：{relative}")
            source_directories.add(relative)
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in files:
            item = root_path / name
            relative = relative_root / name
            if _is_preserved_relative(relative):
                continue
            if item.is_symlink():
                raise UpdaterError(f"更新内容不允许文件链接：{relative}")
            source_files.add(relative)

    for relative in sorted(source_directories, key=lambda item: len(item.parts)):
        if not relative.parts:
            continue
        target = destination / relative
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            _remove_tree_entry(target)
        target.mkdir(parents=True, exist_ok=True)

    completion_marker = Path(".aalc-release.json")

    for relative in sorted(source_files - {completion_marker}, key=lambda item: item.as_posix().casefold()):
        _copy_file_atomically(source / relative, destination / relative)

    for root, directories, files in os.walk(destination, topdown=False, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(destination)
        for name in files:
            relative = relative_root / name
            if _is_preserved_relative(relative):
                continue
            if relative not in source_files:
                _remove_tree_entry(root_path / name)
        for name in directories:
            relative = relative_root / name
            target = root_path / name
            if _is_preserved_relative(relative):
                continue
            if target.is_symlink():
                if relative not in source_directories:
                    _remove_tree_entry(target)
                continue
            if relative not in source_directories:
                try:
                    target.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    # 其他程序仅把空目录当作当前工作目录时，Windows 会拒绝删除；
                    # 空目录残留不影响新版本，不能因此把已完成的文件事务判失败。
                    try:
                        is_empty = not any(target.iterdir())
                    except OSError:
                        is_empty = False
                    if is_empty:
                        print(f"保留暂时被占用的空旧目录：{target}")
                    else:
                        raise

    # 完成标记必须最后提交，避免外部程序在主文件尚未替换完时误判更新成功。
    if completion_marker in source_files:
        _copy_file_atomically(source / completion_marker, destination / completion_marker)


def _install_in_place_with_backup(install_dir: Path, staging: Path, backup: Path) -> None:
    """完整备份后执行文件级事务；安装根目录始终保持原位。"""
    try:
        shutil.copytree(install_dir, backup)
    except Exception as exc:
        shutil.rmtree(backup, ignore_errors=True)
        raise UpdaterError(f"无法创建完整备份，尚未修改原目录：{exc}") from exc

    try:
        _mirror_tree(staging, install_dir)
    except Exception as install_exc:
        try:
            _mirror_tree(backup, install_dir)
        except Exception as rollback_exc:
            raise UpdaterError(
                f"原目录兼容安装失败且自动回滚失败；旧版本完整备份位于 {backup}：{rollback_exc}"
            ) from install_exc
        raise UpdaterError(
            f"原目录兼容安装失败，已从完整备份恢复旧版本；备份位于 {backup}：{install_exc}"
        ) from install_exc


def launch_entrypoint(install_dir: Path) -> bool:
    """启动更新后的 AALC；需要管理员权限时改用 ShellExecute 请求 UAC。"""
    executable = install_dir / ENTRYPOINT
    try:
        subprocess.Popen([executable], cwd=install_dir)
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ELEVATION_REQUIRED:
            print(f"更新已完成，但未能自动启动 AALC，请手动运行 {ENTRYPOINT}：{exc}")
            return False

    print("AALC 需要管理员权限，正在请求 UAC 授权……")
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            str(executable),
            None,
            str(install_dir),
            SW_SHOWNORMAL,
        )
        if result <= SHELL_EXECUTE_SUCCESS:
            print(f"更新已完成，但未自动启动 AALC，请手动运行 {ENTRYPOINT}（系统返回值：{result}）")
            return False
        return True
    except Exception as exc:
        # 安装事务已经提交，UAC 被取消或启动 API 异常都不应再把更新判为失败。
        print(f"更新已完成，但未自动启动 AALC，请手动运行 {ENTRYPOINT}：{exc}")
        return False


def transactional_install(
    install_dir: Path,
    payload_root: Path,
    version: str,
    *,
    launch: bool = True,
) -> Path:
    install_dir = install_dir.resolve()
    if not (install_dir / ENTRYPOINT).is_file():
        raise UpdaterError(f"更新程序必须放在 AALC 目录内；当前目录缺少 {ENTRYPOINT}")
    if install_dir.parent == install_dir:
        raise UpdaterError("不能更新磁盘根目录")

    suffix = time.strftime("%Y%m%d-%H%M%S")
    staging = install_dir.parent / f".{install_dir.name}.update-staging-{uuid.uuid4().hex[:8]}"
    backup = install_dir.parent / f"{install_dir.name}.backup-{suffix}"
    if backup.exists():
        backup = install_dir.parent / f"{backup.name}-{uuid.uuid4().hex[:4]}"

    try:
        print("正在准备新版本……")
        shutil.copytree(payload_root, staging)
        (staging / ".aalc-release.json").write_text(
            json.dumps({"version": version, "repository": REPOSITORY}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stop_target_processes(install_dir)

        print("正在创建完整回滚备份……")
        print("正在执行文件级原子更新（AALC 根目录不会移动）……")
        _install_in_place_with_backup(install_dir, staging, backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    print(f"更新完成：{version}")
    print(f"旧版本备份：{backup}")
    if launch:
        launch_entrypoint(install_dir)
    return backup


class StandaloneUpdater:
    def __init__(self, install_dir: Path, api_url: str = LATEST_RELEASE_API):
        self.install_dir = install_dir.resolve()
        self.api_url = api_url

    def _load_release(self) -> tuple[str, dict[str, ReleaseAsset], UpdateManifest]:
        print(f"正在检查 {REPOSITORY} 的最新版本……")
        try:
            tag, assets = parse_release(fetch_json(self.api_url))
            manifest_asset = assets.get(MANIFEST_ASSET_NAME)
            if manifest_asset is None:
                raise UpdaterError(f"最新 Release 缺少 {MANIFEST_ASSET_NAME}")
            manifest = UpdateManifest.from_json(fetch_json(manifest_asset.url), tag)
            return tag, assets, manifest
        except UpdateNetworkError as api_error:
            print(f"GitHub API 暂不可用，改用公开 Release 直链：{api_error}")

        manifest = UpdateManifest.from_latest_json(fetch_json(LATEST_MANIFEST_URL))
        package = ReleaseAsset(
            manifest.asset,
            release_asset_url(manifest.version, manifest.asset),
            manifest.size,
        )
        return manifest.version, {manifest.asset: package}, manifest

    def run(
        self,
        *,
        source_archive: Path | None = None,
        check_only: bool = False,
        launch: bool = True,
        force: bool = False,
    ) -> Path | None:
        if source_archive is None:
            tag, assets, manifest = self._load_release()
            package_asset = assets.get(manifest.asset)
            if package_asset is None:
                raise UpdaterError(f"最新 Release 缺少 {manifest.asset}")
            if manifest.size is not None and package_asset.size is not None and manifest.size != package_asset.size:
                raise UpdaterError("Release 资产大小与更新清单不一致")
        else:
            print(f"正在读取本地更新包：{source_archive}")
            manifest = inspect_local_archive(source_archive)
            tag = manifest.version
            package_asset = ReleaseAsset(manifest.asset, "", manifest.size)
        if check_only:
            print(f"最新可用版本：{tag}")
            return None
        local_release = self.install_dir / ".aalc-release.json"
        if not force and local_release.is_file():
            try:
                current_version = json.loads(local_release.read_text(encoding="utf-8")).get("version")
            except (OSError, json.JSONDecodeError, AttributeError):
                current_version = None
            if current_version == tag:
                print(f"当前已经是最新版本：{tag}")
                return None

        workspace = Path(tempfile.mkdtemp(prefix="AALC-update-payload-"))
        cached_package: Path | None = None
        try:
            if source_archive is None:
                # 下载缓存放在当前 AALC 目录的独立子目录中，跨进程/跨次运行保留 .part，
                # 网络中断后可从断点续传；文件级安装会跳过该目录，不会覆盖它。
                cache_dir = self.install_dir / "update_temp"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached_package = cache_dir / manifest.asset
                package_path = cached_package
                print(f"正在下载 {manifest.asset}……")
                download_file(package_asset.url, package_path, manifest.size or package_asset.size)
            else:
                print(f"正在验证本地更新包：{source_archive}")
                # 本地 ZIP 已由用户放在目标目录，直接读取它，避免复制 200MB+ 文件到
                # 系统临时目录；更新镜像会跳过 AALC-Optimized*.zip，因此源包不会被删除。
                package_path = source_archive.resolve()
            actual_hash = sha256_file(package_path)
            if actual_hash != manifest.sha256:
                if cached_package is not None:
                    cached_package.unlink(missing_ok=True)
                raise UpdaterError(f"更新包 SHA-256 校验失败：{actual_hash}")
            print("SHA-256 校验通过")

            extracted = workspace / "extracted"
            payload_root = extract_verified_zip(package_path, extracted, manifest)
            return transactional_install(self.install_dir, payload_root, tag, launch=launch)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


def _executable_path() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()


def resolve_install_dir(args: argparse.Namespace, executable: Path) -> Path:
    """双击模式只认更新器所在目录；目标参数仅供复制到临时目录后的内部进程使用。"""
    if args.worker:
        if args.install_dir is None:
            raise UpdaterError("更新器内部进程缺少原始 AALC 目录")
        return args.install_dir.resolve()
    if args.install_dir is not None:
        raise UpdaterError("不能从命令行指定更新目录；请把更新程序放进要更新的 AALC 文件夹")
    return executable.parent.resolve()


def _resolve_legacy_archive(install_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = install_dir / "update_temp" / candidate.name
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise UpdaterError(f"找不到已下载的更新包：{candidate}")
    return candidate


def find_local_archive(install_dir: Path) -> Path:
    archive = (install_dir / DEFAULT_PACKAGE_ASSET).resolve()
    if archive.is_file():
        return archive

    candidates = sorted(
        (
            item.resolve()
            for item in install_dir.glob("AALC-Optimized*.zip")
            if item.is_file() and _SAFE_ZIP_ASSET.fullmatch(item.name) is not None
        ),
        key=lambda item: item.name.casefold(),
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = "、".join(item.name for item in candidates)
        raise UpdaterError(f"当前目录有多个更新包，无法安全判断要安装哪一个：{names}")
    raise UpdaterError(
        f"请先下载 {DEFAULT_PACKAGE_ASSET}，把它放到当前 AALC 文件夹后再双击 AALC-Update.exe；"
        f"应放置在：{archive}"
    )


def relaunch_worker(args: argparse.Namespace, install_dir: Path, source_archive: Path | None) -> None:
    if not getattr(sys, "frozen", False):
        raise UpdaterError("源码运行请使用 --no-relaunch；给普通电脑使用时请运行打包后的 EXE")
    workspace = Path(tempfile.mkdtemp(prefix="AALC-updater-worker-"))
    worker = workspace / "AALC 更新程序.exe"
    shutil.copy2(_executable_path(), worker)
    command = [
        str(worker),
        "--worker",
        "--parent-pid",
        str(os.getpid()),
        "--install-dir",
        str(install_dir),
        "--api-url",
        args.api_url,
    ]
    if source_archive is not None:
        copied_archive = workspace / source_archive.name
        shutil.copy2(source_archive, copied_archive)
        command.extend(["--source-archive", str(copied_archive)])
    if args.check_only:
        command.append("--check-only")
    if args.no_launch:
        command.append("--no-launch")
    if args.force:
        command.append("--force")
    subprocess.Popen(command, cwd=workspace, creationflags=CREATE_NEW_CONSOLE, close_fds=True)


def wait_for_parent_exit(parent_pid: int | None, timeout: float = 15.0) -> None:
    """等待安装目录中的原更新器退出，避免 Windows 文件占用导致目录切换失败。"""
    if not parent_pid:
        return
    try:
        psutil.Process(parent_pid).wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise UpdaterError("旧更新程序未能退出，请稍后重新运行更新") from exc


def _show_error(message: str) -> None:
    print(f"\n更新失败：{message}")
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "AALC 更新失败", 0x10)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AALC Optimized 零配置更新程序")
    parser.add_argument("legacy_archive", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--install-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--source-archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--api-url", default=LATEST_RELEASE_API, help=argparse.SUPPRESS)
    parser.add_argument("--check-only", action="store_true", help="只检查最新版本")
    parser.add_argument("--no-launch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-relaunch", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    executable = _executable_path()
    try:
        install_dir = resolve_install_dir(args, executable)
        print(f"本次只更新此目录：{install_dir}")
        source_archive = args.source_archive or _resolve_legacy_archive(install_dir, args.legacy_archive)
        if source_archive is None and not args.worker and not args.check_only:
            source_archive = find_local_archive(install_dir)
        if not args.worker and not args.no_relaunch:
            if not (install_dir / ENTRYPOINT).is_file():
                raise UpdaterError(f"请把更新程序放入 AALC 文件夹后再双击；此处缺少 {ENTRYPOINT}")
            relaunch_worker(args, install_dir, source_archive)
            return 0
        wait_for_parent_exit(args.parent_pid)
        StandaloneUpdater(install_dir, args.api_url).run(
            source_archive=source_archive,
            check_only=args.check_only,
            launch=not args.no_launch,
            force=args.force,
        )
        return 0
    except UpdaterError as exc:
        _show_error(str(exc))
        return 1
    except Exception as exc:
        _show_error(f"未预期错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
