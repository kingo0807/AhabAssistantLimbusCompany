"""从已构建的 AALC 目录创建可校验的零配置更新资产。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()

    app_dir = args.dist / "AALC"
    if not (app_dir / "AALC.exe").is_file() or not (app_dir / "AALC Updater.exe").is_file():
        raise FileNotFoundError("dist/AALC 中缺少 AALC.exe 或 AALC Updater.exe")

    package_path = args.dist / "AALC-Optimized-win64.zip"
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as package:
        for path in app_dir.rglob("*"):
            if path.is_file():
                package.write(path, path.relative_to(args.dist).as_posix())

    manifest = {
        "schema_version": 1,
        "version": args.version,
        "asset": package_path.name,
        "sha256": sha256_file(package_path),
        "size": package_path.stat().st_size,
        "entrypoint": "AALC.exe",
        "archive_root": "AALC",
        "min_updater_version": "1.0.0",
    }
    (args.dist / "update-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(app_dir / "AALC Updater.exe", args.dist / "AALC-Update.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
