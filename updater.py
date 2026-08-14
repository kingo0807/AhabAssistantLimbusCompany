from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import psutil

UPDATER_VERSION = "1.2.0"
REPOSITORY = "kingo0807/AhabAssistantLimbusCompany"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MANIFEST_ASSET_NAME = "update-manifest.json"
DEFAULT_PACKAGE_ASSET = "AALC-Optimized-win64.zip"
ENTRYPOINT = "AALC.exe"
USER_AGENT = f"AALC-Optimized-Updater/{UPDATER_VERSION}"
PRESERVE_FILES = ("config.yaml", "theme_pack_list.yaml")
PRESERVE_GLOBS = ("config.yaml.*",)
PRESERVE_DIRECTORIES = ("config_backup", "logs", "pythonlogs", "theme_pack_weight")
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
ERROR_ELEVATION_REQUIRED = 740
SHELL_EXECUTE_SUCCESS = 32
SW_SHOWNORMAL = 1


class UpdaterError(RuntimeError):
    """更新无法安全完成。"""


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

        version = payload.get("version")
        asset = payload.get("asset")
        sha256 = payload.get("sha256")
        size = payload.get("size")
        entrypoint = payload.get("entrypoint", ENTRYPOINT)
        archive_root = payload.get("archive_root", "AALC")

        if version != release_tag:
            raise UpdaterError(f"更新清单版本 {version!r} 与 Release {release_tag!r} 不一致")
        if not isinstance(asset, str) or not asset.lower().endswith(".zip"):
            raise UpdaterError("更新清单没有有效的 ZIP 资产名")
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


def _request(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/octet-stream",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_json(url: str) -> Any:
    try:
        with _request(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpdaterError(f"无法读取更新信息：{exc}") from exc


def download_file(url: str, destination: Path, expected_size: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    downloaded = 0
    last_percent = -1
    try:
        with _request(url, timeout=60) as response, partial.open("wb") as output:
            response_size = response.headers.get("Content-Length")
            total = expected_size or (int(response_size) if response_size and response_size.isdigit() else None)
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = min(100, downloaded * 100 // total)
                    if percent // 5 != last_percent // 5:
                        print(f"下载进度：{percent}%")
                        last_percent = percent
        if expected_size is not None and downloaded != expected_size:
            raise UpdaterError(f"下载大小不符：应为 {expected_size}，实际为 {downloaded}")
        os.replace(partial, destination)
    except UpdaterError:
        partial.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdaterError(f"下载更新包失败：{exc}") from exc


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


def _same_or_child(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def stop_target_processes(install_dir: Path) -> None:
    targets: list[psutil.Process] = []
    current_pid = os.getpid()
    for process in psutil.process_iter(["pid", "name", "exe"]):
        if process.info["pid"] == current_pid or (process.info.get("name") or "").casefold() != ENTRYPOINT.casefold():
            continue
        try:
            executable = process.info.get("exe") or process.exe()
            if executable and _same_or_child(Path(executable), install_dir):
                targets.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue

    if not targets:
        return
    print(f"正在关闭当前目录中的 {len(targets)} 个 AALC 进程……")
    for process in targets:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = psutil.wait_procs(targets, timeout=10)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    if alive:
        raise UpdaterError("AALC 仍在运行，请手动关闭当前目录中的 AALC 后重试")


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

    old_moved = False
    new_installed = False
    try:
        print("正在准备新版本……")
        shutil.copytree(payload_root, staging)
        (staging / ".aalc-release.json").write_text(
            json.dumps({"version": version, "repository": REPOSITORY}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stop_target_processes(install_dir)
        _copy_preserved_data(install_dir, staging)

        print("正在切换版本……")
        install_dir.rename(backup)
        old_moved = True
        staging.rename(install_dir)
        new_installed = True
    except Exception as exc:
        if new_installed and install_dir.exists():
            failed = install_dir.parent / f".{install_dir.name}.failed-{uuid.uuid4().hex[:8]}"
            try:
                install_dir.rename(failed)
                shutil.rmtree(failed, ignore_errors=True)
            except OSError:
                pass
        if old_moved and backup.exists() and not install_dir.exists():
            try:
                backup.rename(install_dir)
            except OSError as rollback_exc:
                raise UpdaterError(f"安装失败且自动回滚失败；旧版本位于 {backup}：{rollback_exc}") from exc
        if isinstance(exc, UpdaterError):
            raise
        raise UpdaterError(f"安装失败，已恢复旧版本：{exc}") from exc
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
        tag, assets = parse_release(fetch_json(self.api_url))
        manifest_asset = assets.get(MANIFEST_ASSET_NAME)
        if manifest_asset is None:
            raise UpdaterError(f"最新 Release 缺少 {MANIFEST_ASSET_NAME}")
        manifest = UpdateManifest.from_json(fetch_json(manifest_asset.url), tag)
        return tag, assets, manifest

    def run(
        self,
        *,
        source_archive: Path | None = None,
        check_only: bool = False,
        launch: bool = True,
        force: bool = False,
    ) -> Path | None:
        tag, assets, manifest = self._load_release()
        package_asset = assets.get(manifest.asset)
        if package_asset is None:
            raise UpdaterError(f"最新 Release 缺少 {manifest.asset}")
        if manifest.size is not None and package_asset.size is not None and manifest.size != package_asset.size:
            raise UpdaterError("Release 资产大小与更新清单不一致")
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
        try:
            package_path = workspace / manifest.asset
            if source_archive is None:
                print(f"正在下载 {manifest.asset}……")
                download_file(package_asset.url, package_path, manifest.size or package_asset.size)
            else:
                print(f"正在验证已下载的更新包：{source_archive}")
                shutil.copy2(source_archive, package_path)
            actual_hash = sha256_file(package_path)
            if actual_hash != manifest.sha256:
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
