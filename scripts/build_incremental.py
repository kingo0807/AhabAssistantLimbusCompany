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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import requests

DEFAULT_MANIFEST = "https://dl.wangyuyue.xyz/aalc/latest.json"
USER_AGENT = {"User-Agent": "AALC-build-incremental/1.0"}

# Windows runner 上 stdout 默认是 cp1252，中文日志会直接 UnicodeEncodeError 把脚本打死。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


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


def announce_error(message: str) -> None:
    """真正的异常用 ::error:: 报出来，避免 continue-on-error 把它藏成绿色。"""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message}", flush=True)
    else:
        print(f"ERROR: {message}", flush=True)


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


def version_key(version: str) -> tuple[int, ...]:
    """把 V1.6.7-dev 之类解析成 (1, 6, 7)，用于比较 dist release 的先后。"""
    match = re.search(r"(\d+(?:\.\d+)*)", version)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _base_short(base_version: str) -> str:
    """V1.6.26-dev -> 1.6.26，用来给非主基线的补丁文件名做后缀。"""
    match = re.search(r"(\d+(?:\.\d+)*)", base_version or "")
    return match.group(1) if match else "base"


def patch_file_name(stem: str, base_version: str, primary: bool) -> str:
    """主基线沿用旧文件名（AALC_<新版本>.patch.7z），其它基线带 from-<版本> 后缀。"""
    if primary:
        return f"{stem}.patch.7z"
    return f"{stem}.patch.from-{_base_short(base_version)}.7z"


def find_dist_release_bases(repo: str, token: str, new_version: str, limit: int = 2):
    """在 GitHub 的 dist-* release 里找比 new_version 小的、最高的 limit 个版本。

    返回 [(版本号, 全量包下载地址), ...]，从新到旧；没有可用基线时返回空列表。
    这里只认 dist-* 前缀，避免把仓库里其它 v1.0.9.x 预发布当基线。
    """
    headers = dict(USER_AGENT)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(
        f"https://api.github.com/repos/{repo}/releases?per_page=100",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    new_key = version_key(new_version)
    found: list[tuple[tuple[int, ...], str, str]] = []
    for release in response.json():
        tag = str(release.get("tag_name") or "")
        if not tag.startswith("dist-") or release.get("draft"):
            continue
        version = tag[len("dist-"):]
        key = version_key(version)
        if not key or key >= new_key:
            continue
        full_asset = next(
            (
                asset
                for asset in release.get("assets", [])
                if asset.get("name", "").endswith(".7z") and ".patch." not in asset["name"]
            ),
            None,
        )
        if not full_asset:
            continue
        found.append((key, version, full_asset["browser_download_url"]))

    found.sort(key=lambda item: item[0], reverse=True)
    return [(version, url) for _, version, url in found[: max(1, limit)]]



def run_seven_zip(args: list[str], cwd: Path | None = None) -> None:
    """调用 7z；失败时把 stderr 带出来，别让它静默变成一句 FileNotFoundError。"""
    result = subprocess.run(
        [SEVEN_ZIP, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"7z {args[0]} 失败（exit {result.returncode}）："
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )


def extract(package: Path, dest: Path) -> Path:
    """解压 7z 到 dest，返回归档根目录（单顶层目录时就是那个目录）。"""
    dest.mkdir(parents=True, exist_ok=True)
    run_seven_zip(["x", "-y", f"-o{dest}", str(package.resolve())])
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
    # 7z 的 cwd 是 staging，输出路径必须绝对，否则会落到 staging\dist\ 下
    run_seven_zip(
        ["a", "-t7z", "-mx=7", str(out_package.resolve()), "*"],
        cwd=staging,
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
    run_seven_zip(["x", "-y", f"-o{unpacked}", str(patch_package.resolve())])

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
    parser.add_argument(
        "--base-repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="从这个仓库的 dist-* release 取基线（优先于清单下载）",
    )
    parser.add_argument(
        "--base-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="访问 GitHub API 的 token（CI 里由 GITHUB_TOKEN 提供）",
    )
    parser.add_argument("--verify", action="store_true", help="应用补丁并与新版本逐字节比对")
    parser.add_argument("--keep-work", action="store_true", help="保留临时目录（调试用）")
    parser.add_argument(
        "--bases",
        type=int,
        default=1,
        help="给最近几个已发布版本各出一个补丁：1=只给上一版，2=上一版 + 上上版",
    )
    args = parser.parse_args()

    new_package = Path(args.new).resolve()
    if not new_package.is_file():
        print(f"找不到新包：{new_package}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名形如 AALC_V1.6.5-dev.7z，去掉 .7z 后缀就是补丁包的前缀
    stem = new_package.stem
    new_version = stem[len("AALC_"):] if stem.startswith("AALC_") else stem

    work = Path(tempfile.mkdtemp(prefix="aalc-patch-"))

    try:
        log(f"临时目录：{work}")

        # 第一步：确定基线候选（从新到旧）。多基线是为了让「跳过一版」的客户端也能走补丁。
        bases: list[tuple[str, str, Path | None]] = []
        if args.base_package:
            local_base = Path(args.base_package).resolve()
            bases.append(("", "", local_base))
            log(f"使用本地基线包：{local_base}")
        elif args.base_repo:
            try:
                for version, url in find_dist_release_bases(
                    args.base_repo, args.base_token, new_version, args.bases
                ):
                    bases.append((version, url, None))
                    log(f"使用 GitHub dist release 基线：{version}")
                if not bases:
                    warn("dist release 里没有更早的版本，回退到清单下载基线")
            except Exception as exc:
                warn(f"从 GitHub dist release 取基线失败，回退到清单：{exc!r}")

        if not bases:
            response = requests.get(args.manifest, timeout=20, headers=USER_AGENT)
            response.raise_for_status()
            published = response.json()
            base_version = str(published.get("version") or "")
            base_url = str(published.get("url") or "")
            if not base_url:
                warn("清单里没有 url 字段，跳过增量包生成")
                return 0
            log(f"当前已发布版本：{base_version}")
            bases.append((base_version, base_url, None))

        # 新版本只解压一次，所有基线共用
        log("解压新版本...")
        new_root = extract(new_package, work / "new")
        new_top_name = new_root.name if new_root != (work / "new") else ""
        new_manifest = build_manifest(new_root)

        # 第二步：逐个基线算差异、打补丁、自检
        results: list[dict] = []
        for index, (base_version, base_url, local_base) in enumerate(bases):
            patch_package = out_dir / patch_file_name(stem, base_version, index == 0)
            log(f"— 基线 {base_version or '本地包'} → {patch_package.name}")
            base_work = work / f"base{index}"
            base_work.mkdir(parents=True, exist_ok=True)
            base_package = local_base if local_base else base_work / "base.7z"
            if not local_base:
                download(base_url, base_package)

            old_root = extract(base_package, base_work / "old")
            old_manifest = build_manifest(old_root)
            log(f"文件数：old={len(old_manifest)} new={len(new_manifest)}")

            changes = compute_changes(old_manifest, new_manifest)
            payload_bytes = sum(
                new_manifest[path][1] for path in changes["added"] + changes["modified"]
            )
            total_bytes = sum(size for _, size in new_manifest.values())
            for key in PLAN_KEYS:
                log(f"  {key:<11}: {len(changes[key]):5d} 项")

            if not any(changes[key] for key in PLAN_KEYS):
                warn("新旧版本没有差异，跳过这个基线的补丁")
                continue

            log(f"补丁载荷：{payload_bytes / 1048576:.2f} MB / 全量 {total_bytes / 1048576:.2f} MB")
            if total_bytes and payload_bytes / total_bytes > 0.9:
                warn("补丁载荷超过全量包的 90%，这次增量意义不大")

            build_patch(new_root, new_top_name, changes, patch_package, base_work)
            patch_size = patch_package.stat().st_size
            log(f"补丁包：{patch_package}（{patch_size / 1048576:.2f} MB）")

            verify_ok = None
            verify_message = "未执行"
            if args.verify:
                verify_ok, verify_message = verify_patch(
                    old_root, new_root, new_top_name, changes, patch_package, base_work
                )
                (log if verify_ok else warn)(f"验证：{verify_message}")
                if not verify_ok:
                    patch_package.unlink(missing_ok=True)

            results.append(
                {
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
            )

        if not results:
            warn("没有基线产出补丁，跳过元数据")
            return 0

        # 第三步：写元数据。bases 是完整列表，顶层字段保持旧格式（= 主基线），旧脚本照样能读
        meta: dict = {"version": new_version, "bases": results}
        meta.update(results[0])
        meta_path = out_dir / f"{stem}.patch.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"元数据：{meta_path}（{len(results)} 个基线）")

        if results[0].get("verified") is False:
            return 1
        return 0
    except Exception as exc:  # 构建不能因为增量包而失败，但要在界面上看得见
        announce_error(f"增量包生成失败，已跳过：{exc!r}")
        print(traceback.format_exc(), flush=True)
        return 0
    finally:
        if args.keep_work:
            log(f"保留临时目录：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
