"""生成 AALC 的增量更新补丁包。

用法（CI 里由 reusable-build.yml 调用）：

    uv run python ./scripts/build_incremental.py \
        --new dist/AALC_V1.6.5-dev.7z --out-dir dist --verify

它做四件事：
  1. 从自建清单读当前已发布版本，下载上一版的全量包；
  2. 把新旧两个包解压，逐文件算 sha256 比对；
  3. 产出补丁包（changes.json + 只有新增/修改的文件）和一份 .patch.json 元数据；
  4. 用与客户端相同的规则把补丁套到旧版本拷贝上，逐字节验证结果是否等于新版本。

产物（--out-dir 下）：
    AALC_<新版本>.patch.7z    补丁包
    AALC_<新版本>.patch.json  元数据（发布时写进 latest.json 用）

必须遵守的约束（都来自客户端 updater.py，改之前先读它）：

  * changes.json 必须在**归档根目录**。客户端读的是 ./update_temp/changes.json
    （updater.py:32），而载荷从 ./update_temp/AALC/ 解析（updater.py:200-203）。
  * 载荷必须保留与全量包相同的顶层目录（例如 AALC/），否则源文件解析不到。
  * changes.json 的键只能是 deleted_dir / deleted / added_dir / added / modified。
    执行更新的永远是被替换前那一版的 updater.py，多一个键就会
    UpdateManifestError（updater.py:191-196）。所以这里不能加 base_version、
    不能加每文件哈希，基线信息一律放在 latest.json 的 incremental 块里。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

DEFAULT_MANIFEST = "https://dl.wangyuyue.xyz/aalc/latest.json"
USER_AGENT = {"User-Agent": "AALC-build-incremental/1.0"}


def _find_seven_zip() -> str:
    """优先用环境变量，其次 PATH，最后 Windows 默认安装位置。"""
    explicit = os.environ.get("SEVEN_ZIP_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if found:
        return found
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(candidate).is_file():
            return candidate
    return "7z"  # 交给子进程抛错，main 里会转成警告


SEVEN_ZIP = _find_seven_zip()

# changes.json 只允许这五个键，顺序也固定，方便人看
PLAN_KEYS = ("deleted_dir", "deleted", "added_dir", "added", "modified")


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    """CI 里输出成 annotation，本地输出成普通警告。"""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}", flush=True)
    else:
        print(f"WARNING: {message}", flush=True)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def download(url: str, dest: Path) -> None:
    started = time.time()
    with requests.get(url, stream=True, timeout=30, headers=USER_AGENT) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        written = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    log(f"下载上一版包 {url} -> {written / 1048576:.1f} MB，{time.time() - started:.1f}s（声明 {total} 字节）")


def extract(package: Path, dest: Path) -> Path:
    """解压 7z 到 dest，返回归档根目录（单顶层目录时就是那个目录）。"""
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [SEVEN_ZIP, "x", "-y", f"-o{dest}", str(package)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    entries = list(dest.iterdir())
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    # 归档是“散”的（顶层直接是文件），根目录就是解压目录本身
    return dest


def build_manifest(root: Path) -> dict[str, tuple[str, int]]:
    """相对路径 -> (sha256, 字节数)，路径统一用 / 分隔。"""
    result: dict[str, tuple[str, int]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (sha256_file(path), path.stat().st_size)
    return result


def dir_set(manifest: dict[str, tuple[str, int]]) -> set[str]:
    dirs: set[str] = set()
    for path in manifest:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:index]))
    return dirs


def prune_dirs(dirs: set[str]) -> list[str]:
    """去掉“父目录已被列入”的子目录，避免重复创建/删除。"""
    pruned: list[str] = []
    for entry in sorted(dirs):
        if not any(entry.startswith(parent + "/") for parent in pruned):
            pruned.append(entry)
    return pruned


def compute_changes(old: dict[str, tuple[str, int]], new: dict[str, tuple[str, int]]) -> dict[str, list[str]]:
    added = sorted(path for path in new if path not in old)
    deleted = sorted(path for path in old if path not in new)
    modified = sorted(path for path in new if path in old and old[path][0] != new[path][0])

    old_dirs = dir_set(old)
    new_dirs = dir_set(new)

    return {
        "deleted_dir": prune_dirs(old_dirs - new_dirs),
        "deleted": deleted,
        "added_dir": prune_dirs(new_dirs - old_dirs),
        "added": added,
        "modified": modified,
    }


def write_changes_json(path: Path, changes: dict[str, list[str]]) -> None:
    ordered = {key: changes[key] for key in PLAN_KEYS}
    path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_patch(
    new_root: Path,
    new_top_name: str,
    changes: dict[str, list[str]],
    out_package: Path,
    work: Path,
) -> None:
    """按“归档根目录放 changes.json、载荷放在新包同名顶层目录下”组装补丁包。"""
    staging = work / "patch"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    write_changes_json(staging / "changes.json", changes)

    payload_root = staging / new_top_name
    for relative in changes["added"] + changes["modified"]:
        source = new_root / relative
        target = payload_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in changes["added_dir"]:
        (payload_root / relative).mkdir(parents=True, exist_ok=True)

    if out_package.exists():
        out_package.unlink()
    subprocess.run(
        [SEVEN_ZIP, "a", "-t7z", "-mx=7", str(out_package), "*"],
        cwd=staging,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def apply_changes(install_root: Path, payload_root: Path, changes: dict[str, list[str]]) -> None:
    """按客户端 updater.py 的语义把补丁套到安装目录（顺序一致：先删后加）。"""
    for relative in changes["deleted_dir"]:
        shutil.rmtree(install_root / relative, ignore_errors=True)
    for relative in changes["deleted"]:
        try:
            (install_root / relative).unlink()
        except FileNotFoundError:
            pass
    for relative in changes["added_dir"]:
        (install_root / relative).mkdir(parents=True, exist_ok=True)
    for relative in changes["added"] + changes["modified"]:
        source = payload_root / relative
        target = install_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def verify_patch(
    old_root: Path,
    new_root: Path,
    new_top_name: str,
    changes: dict[str, list[str]],
    patch_package: Path,
    work: Path,
) -> tuple[bool, str]:
    """把补丁套到旧版本拷贝上，逐文件比对结果与新版本是否完全一致。"""
    install = work / "verify-install"
    if install.exists():
        shutil.rmtree(install)
    shutil.copytree(old_root, install)

    unpacked = work / "verify-patch"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [SEVEN_ZIP, "x", "-y", f"-o{unpacked}", str(patch_package)],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    if not (unpacked / "changes.json").is_file():
        return False, "补丁包里没有根目录级的 changes.json"

    payload_root = unpacked / new_top_name
    if not payload_root.is_dir():
        return False, f"补丁包里没有载荷目录 {new_top_name}/"

    apply_changes(install, payload_root, changes)

    got = build_manifest(install)
    want = build_manifest(new_root)
    if got.keys() != want.keys():
        missing = sorted(set(want) - set(got))[:5]
        extra = sorted(set(got) - set(want))[:5]
        return False, f"文件集合不一致，缺少 {missing}，多出 {extra}"
    mismatched = [path for path in want if got[path][0] != want[path][0]]
    if mismatched:
        return False, f"内容不一致：{mismatched[:5]}"
    return True, "补丁应用后与新版本逐字节一致"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AALC 增量更新补丁")
    parser.add_argument("--new", required=True, help="新构建出来的全量包路径")
    parser.add_argument("--out-dir", default="dist", help="补丁产物输出目录")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="自建更新清单地址")
    parser.add_argument("--base-package", default=None, help="本地已有的上一版包（跳过下载，调试用）")
    parser.add_argument("--verify", action="store_true", help="应用补丁并与新版本逐字节比对")
    parser.add_argument("--keep-work", action="store_true", help="保留临时目录（调试用）")
    args = parser.parse_args()

    new_package = Path(args.new).resolve()
    if not new_package.is_file():
        print(f"找不到新包：{new_package}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名形如 AALC_V1.6.5-dev.7z，去掉 .7z 后缀就是补丁包的前缀
    stem = new_package.stem
    patch_package = out_dir / f"{stem}.patch.7z"
    meta_path = out_dir / f"{stem}.patch.json"

    work = Path(tempfile.mkdtemp(prefix="aalc-patch-"))
    log(f"临时目录：{work}")

    try:
        # 第一步：拿到上一版包
        base_version = ""
        base_url = ""
        if args.base_package:
            base_package = Path(args.base_package).resolve()
            log(f"使用本地基线包：{base_package}")
        else:
            response = requests.get(args.manifest, timeout=20, headers=USER_AGENT)
            response.raise_for_status()
            published = response.json()
            base_version = str(published.get("version") or "")
            base_url = str(published.get("url") or "")
            if not base_url:
                warn("清单里没有 url 字段，跳过增量包生成")
                return 0
            log(f"当前已发布版本：{base_version}")
            base_package = work / "base.7z"
            download(base_url, base_package)

        # 第二步：解压 + 建索引
        log("解压上一版...")
        old_root = extract(base_package, work / "old")
        log("解压新版本...")
        new_root = extract(new_package, work / "new")

        old_manifest = build_manifest(old_root)
        new_manifest = build_manifest(new_root)
        log(f"文件数：old={len(old_manifest)} new={len(new_manifest)}")

        # 第三步：算差异
        changes = compute_changes(old_manifest, new_manifest)
        payload_bytes = sum(new_manifest[path][1] for path in changes["added"] + changes["modified"])
        total_bytes = sum(size for _, size in new_manifest.values())
        for key in PLAN_KEYS:
            log(f"  {key:<11}: {len(changes[key]):5d} 项")

        if not changes["added"] and not changes["modified"] and not changes["deleted"] \
                and not changes["deleted_dir"] and not changes["added_dir"]:
            warn("新旧版本没有差异，跳过补丁生成")
            return 0

        log(f"补丁载荷：{payload_bytes / 1048576:.2f} MB / 全量 {total_bytes / 1048576:.2f} MB")
        if total_bytes and payload_bytes / total_bytes > 0.9:
            warn("补丁载荷超过全量包的 90%，这次增量意义不大")

        # 第四步：打包
        # 顶层目录名取自新包（正常就是 AALC），补丁里保持同名结构
        new_top_name = new_root.name if new_root != (work / "new") else ""
        build_patch(new_root, new_top_name, changes, patch_package, work)
        patch_size = patch_package.stat().st_size
        log(f"补丁包：{patch_package}（{patch_size / 1048576:.2f} MB）")

        # 第五步：验证
        verify_ok = None
        verify_message = "未执行"
        if args.verify:
            verify_ok, verify_message = verify_patch(
                old_root, new_root, new_top_name, changes, patch_package, work
            )
            (log if verify_ok else warn)(f"验证：{verify_message}")
            if not verify_ok:
                patch_package.unlink(missing_ok=True)

        # 第六步：写出元数据，发布时直接读它填 latest.json
        meta = {
            "version": stem[len("AALC_"):] if stem.startswith("AALC_") else stem,
            "base_version": base_version,
            "base_url": base_url,
            "patch_file": patch_package.name,
            "patch_size": patch_size,
            "patch_sha256": sha256_file(patch_package) if patch_package.exists() else "",
            "payload_bytes": payload_bytes,
            "full_tree_bytes": total_bytes,
            "verified": verify_ok,
            "verify_message": verify_message,
            "counts": {key: len(changes[key]) for key in PLAN_KEYS},
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"元数据：{meta_path}")

        if verify_ok is False:
            return 1
        return 0
    except Exception as exc:  # 构建不能因为增量包而失败
        warn(f"增量包生成失败，已跳过：{exc!r}")
        return 0
    finally:
        if args.keep_work:
            log(f"保留临时目录：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
