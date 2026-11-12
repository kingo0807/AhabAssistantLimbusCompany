#!/usr/bin/env python3
"""把 GitHub 上最新的 dist-* release 同步到本地分发目录。

部署位置：
  * 家里 NanoKVM Go：/usr/local/bin/aalc-sync.py，由 aalc-sync.timer 每 30 分钟跑；
    同步完调用 aalc-manifest.sh 把 latest.json 里的主机名换成设备 IP。
  * 办公室虚拟机：同样一份脚本，--dest /opt/aalc-dist/aalc，不跑 manifest
    （公网 nginx 直接按 dl.wangyuyue.xyz 的地址提供）。

用法：
    python3 aalc_sync.py --dest /srv/aalc --manifest-cmd /usr/local/bin/aalc-manifest.sh
    python3 aalc_sync.py --dest /opt/aalc-dist/aalc --proxies ""

逻辑：
  1. 查 GitHub API，取 dist-* 里版本最高的 release（排除 draft）；
  2. 先下载它的小文件 latest.json，和本地 latest.json.src 逐字节比较——
     一样就说明不用动；同版本重跑但内容变了也能识别；
  3. 全量包必须存在，按 latest.json 里的 sha256/size 校验；
  4. 补丁包 + patch.json 能拿到且校验通过才保留 incremental 块，否则剥掉，
     让客户端回落到全量包，绝不发一个指向空文件的清单；
  5. 所有文件先在 .aalc-sync-tmp 里下好、校验完，再 os.replace 原子落位；
  6. 按 --keep 清理旧版本（默认留 3 个），最后按需执行 manifest 命令。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_REPO = "kingo0807/AhabAssistantLimbusCompany"
DEFAULT_PROXIES = "https://ghfast.top/,https://gh-proxy.com/"
USER_AGENT = "AALC-sync/1.0"
TMP_DIR_NAME = ".aalc-sync-tmp"

try:  # Windows 本地调试没有 fcntl，直接跳过着锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def log(message: str) -> None:
    print(f"[{time.strftime('%F %T')}] {message}", flush=True)


def version_key(version: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", version or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def http_json(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, dest: Path, timeout: int = 3600) -> None:
    """流式下载，每 20MB 打一行进度。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.time()
    written = 0
    next_report = 20 * 1024 * 1024
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as f:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            if written >= next_report:
                log(f"  {dest.name}: {written / 1048576:.0f} MB（{written / max(time.time() - started, 0.01) / 1048576:.1f} MB/s）")
                next_report += 20 * 1024 * 1024
    log(f"  {dest.name}: 完成 {written / 1048576:.1f} MB，{time.time() - started:.1f}s")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def pick_release(releases: list[dict]):
    """挑出 dist-* 里版本最高的非 draft release。"""
    best = None
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if not tag.startswith("dist-") or release.get("draft"):
            continue
        version = tag[len("dist-"):]
        key = version_key(version)
        if not key:
            continue
        if best is None or key > best[0]:
            best = (key, version, tag)
    return None if best is None else (best[1], best[2])


def asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def fetch_asset(repo: str, tag: str, name: str, dest: Path, proxies: list[str], timeout: int) -> str:
    """按代理顺序下载一个 release asset，返回实际成功的 URL。"""
    errors = []
    for prefix in proxies:
        url = prefix + asset_url(repo, tag, name)
        try:
            download(url, dest, timeout=timeout)
            return url
        except Exception as exc:  # 换下一个代理
            errors.append(f"{url} -> {exc!r}")
            dest.unlink(missing_ok=True)
    raise RuntimeError("所有下载通道都失败：" + " | ".join(errors))


def local_version(dest: Path) -> str:
    for name in ("latest.json.src", "latest.json"):
        path = dest / name
        if not path.is_file():
            continue
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
        except (json.JSONDecodeError, OSError):
            continue
    return ""


def manifest_up_to_date(dest: Path, remote_latest: Path, data: dict) -> bool:
    """清单逐字节一致、且清单里那版全量包也在且 sha256 对得上，才算不用动。

    家里 KVM 比对 latest.json.src（设备 IP 版由 manifest 生成），
    公网 VM 没有 src，退回比对 latest.json。
    """
    same_manifest = False
    for name in ("latest.json.src", "latest.json"):
        path = dest / name
        if path.is_file() and path.read_bytes() == remote_latest.read_bytes():
            same_manifest = True
            break
    if not same_manifest:
        return False
    full = dest / f"AALC_{data.get('version')}.7z"
    expected = str(data.get("sha256") or "")
    return bool(full.is_file() and expected and sha256_file(full) == expected)


def prune(dest: Path, keep: int) -> None:
    """保留最新 keep 个版本的包和补丁，删掉更早的。"""
    versions: dict[str, list[Path]] = {}
    for path in dest.glob("AALC_*.7z"):
        if ".patch." in path.name:
            continue
        match = re.fullmatch(r"AALC_(.+)\.7z", path.name)
        if match:
            versions.setdefault(match.group(1), []).append(path)
    ordered = sorted(versions, key=version_key, reverse=True)
    for version in ordered[keep:]:
        for path in versions[version]:
            path.unlink(missing_ok=True)
        for suffix in (".patch.7z", ".patch.json"):
            (dest / f"AALC_{version}{suffix}").unlink(missing_ok=True)
        log(f"清理旧版本：{version}")
    for stale in dest.glob(".aalc-sync-tmp*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)


def run(args) -> int:
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    proxies = [p.strip() for p in args.proxies.split(",") if p.strip()]
    proxies.append("")  # 最后回落到直连

    releases = http_json(f"https://api.github.com/repos/{args.repo}/releases?per_page=100")
    target = pick_release(releases)
    if target is None:
        log("没有 dist-* release，跳过")
        return 0
    version, tag = target
    log(f"远端最新：{tag}")

    local = local_version(dest)
    if local and version_key(local) > version_key(version):
        log(f"本地版本 {local} 比远端 {version} 新，跳过")
        return 0

    tmp = dest / TMP_DIR_NAME
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    try:
        latest_path = tmp / "latest.json"
        fetch_asset(args.repo, tag, "latest.json", latest_path, proxies, timeout=120)
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        if str(data.get("version")) != version:
            raise RuntimeError(f"latest.json 版本 {data.get('version')} 与 tag {version} 不一致")

        if manifest_up_to_date(dest, latest_path, data):
            log("本地清单与全量包都和远端一致，无需同步")
            return 0

        full_name = f"AALC_{version}.7z"
        full_in_place = dest / full_name
        expected_sha = str(data.get("sha256") or "")
        downloaded_full = None
        if full_in_place.is_file() and expected_sha and sha256_file(full_in_place) == expected_sha:
            log(f"本地已有校验通过的 {full_name}，跳过下载")
        else:
            downloaded_full = tmp / full_name
            fetch_asset(args.repo, tag, full_name, downloaded_full, proxies, timeout=args.timeout)
            actual_sha = sha256_file(downloaded_full)
            actual_size = downloaded_full.stat().st_size
            expected_size = int(data.get("size") or 0)
            if expected_sha and actual_sha != expected_sha:
                raise RuntimeError(f"{full_name} sha256 不一致：{actual_sha} != {expected_sha}")
            if expected_size and actual_size != expected_size:
                raise RuntimeError(f"{full_name} 大小不一致：{actual_size} != {expected_size}")
            log(f"{full_name} 校验通过")

        # 补丁包：拿不到或校验不过就剥掉 incremental，让客户端用全量包
        incremental = data.get("incremental")
        patch_ok = False
        if isinstance(incremental, dict) and incremental.get("url"):
            patch_name = f"AALC_{version}.patch.7z"
            meta_name = f"AALC_{version}.patch.json"
            try:
                meta_path = tmp / meta_name
                fetch_asset(args.repo, tag, meta_name, meta_path, proxies, timeout=120)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                fetch_asset(args.repo, tag, patch_name, tmp / patch_name, proxies, timeout=args.timeout)
                actual = sha256_file(tmp / patch_name)
                expected = str(meta.get("patch_sha256") or "")
                if not expected or actual != expected:
                    raise RuntimeError(f"{patch_name} sha256 不一致")
                patch_ok = True
                log(f"{patch_name} 校验通过")
            except Exception as exc:
                log(f"补丁同步失败，剥掉 incremental 块：{exc!r}")
                data.pop("incremental", None)

        # 所有文件都在 tmp 里就绪，逐个原子落位
        if downloaded_full is not None:
            os.replace(downloaded_full, full_in_place)
        if patch_ok:
            for name in (f"AALC_{version}.patch.7z", f"AALC_{version}.patch.json"):
                os.replace(tmp / name, dest / name)
        (dest / "latest.json.src").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if args.manifest_cmd:
            log(f"执行 {args.manifest_cmd}")
            subprocess.run([args.manifest_cmd], check=True, timeout=60)
        else:
            shutil.copyfile(dest / "latest.json.src", dest / "latest.json")

        prune(dest, args.keep)
        log(f"同步完成：{version}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步最新 AALC dist release 到本地分发目录")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--manifest-cmd", default="", help="同步成功后执行的命令（例如 aalc-manifest.sh）")
    parser.add_argument("--proxies", default=DEFAULT_PROXIES, help="逗号分隔的 GitHub 反代前缀，空字符串表示只直连")
    parser.add_argument("--keep", type=int, default=3, help="保留最近几个版本的包")
    parser.add_argument("--timeout", type=int, default=3600, help="单个文件下载超时（秒）")
    parser.add_argument("--lock", default="/tmp/aalc-sync.lock")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    lock_handle = None
    if fcntl is not None and not args.dry_run:
        lock_handle = open(args.lock, "w")
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有同步在跑，退出")
            return 0

    try:
        if args.dry_run:
            releases = http_json(f"https://api.github.com/repos/{args.repo}/releases?per_page=100")
            target = pick_release(releases)
            log(f"dry-run：远端 {target}，本地 {local_version(Path(args.dest))}")
            return 0
        return run(args)
    except Exception as exc:
        log(f"同步失败：{exc!r}")
        return 1
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
