#!/usr/bin/env python3
"""从构建产物生成 latest.json（供 dist-* release 和自建更新源使用）。

CI 的 publish-dist 任务在打包完成后调用：

    python3 scripts/make_latest_json.py --version V1.6.8-dev --dir dist \
        --notes-file release_notes.txt

规则：
  * 全量包必须存在，sha256 / size 现场算；
  * 只有补丁包存在、且 build_incremental.py 的自检结果为 verified=true 时，
    才写 incremental 块——补丁错了就不能让客户端去拉；
  * url 默认指向公网自建源；家里 KVM 的 aalc-manifest 会把主机名替换成设备 IP。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_URL_BASE = "https://dl.wangyuyue.xyz/aalc"


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AALC latest.json")
    parser.add_argument("--version", required=True)
    parser.add_argument("--dir", default="dist")
    parser.add_argument("--notes-file", default="release_notes.txt")
    parser.add_argument("--url-base", default=DEFAULT_URL_BASE)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    dist = Path(args.dir)
    version = args.version
    full = dist / f"AALC_{version}.7z"
    patch = dist / f"AALC_{version}.patch.7z"
    meta_path = dist / f"AALC_{version}.patch.json"

    if not full.is_file():
        print(f"找不到全量包：{full}", file=sys.stderr)
        return 1

    notes_path = Path(args.notes_file)
    notes = notes_path.read_text(encoding="utf-8").strip() if notes_path.is_file() else ""

    data = {
        "version": version,
        "notes": notes,
        "url": f"{args.url_base}/AALC_{version}.7z",
        "sha256": sha256_file(full),
        "size": full.stat().st_size,
    }

    if patch.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        if meta.get("verified") is True and meta.get("base_version"):
            data["incremental"] = {
                "base_version": meta["base_version"],
                "url": f"{args.url_base}/AALC_{version}.patch.7z",
            }
        else:
            print("补丁未通过自检或缺少 base_version，不写 incremental 块", file=sys.stderr)

    output = Path(args.output) if args.output else dist / "latest.json"
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
