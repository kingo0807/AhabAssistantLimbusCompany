import hashlib
import zipfile

import pytest

from updater import (
    ENTRYPOINT,
    UpdateManifest,
    UpdaterError,
    extract_verified_zip,
    parse_release,
    transactional_install,
)


def _manifest(*, version="v1.0.0", digest=None):
    return UpdateManifest.from_json(
        {
            "schema_version": 1,
            "version": version,
            "asset": "AALC-Optimized-win64.zip",
            "sha256": digest or "a" * 64,
            "size": 123,
            "entrypoint": ENTRYPOINT,
            "archive_root": "AALC",
        },
        version,
    )


def test_parse_release_collects_only_https_assets():
    tag, assets = parse_release(
        {
            "tag_name": "v1.0.0",
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": "good.zip", "browser_download_url": "https://example/good.zip", "size": 10},
                {"name": "bad.zip", "browser_download_url": "http://example/bad.zip", "size": 10},
            ],
        }
    )

    assert tag == "v1.0.0"
    assert list(assets) == ["good.zip"]


def test_manifest_requires_release_tag_to_match():
    with pytest.raises(UpdaterError, match="不一致"):
        UpdateManifest.from_json(
            {
                "schema_version": 1,
                "version": "v1.0.1",
                "asset": "AALC-Optimized-win64.zip",
                "sha256": "0" * 64,
            },
            "v1.0.0",
        )


def test_extract_verified_zip_rejects_path_escape(tmp_path):
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AALC/AALC.exe", b"new")
        package.writestr("AALC/../../outside.txt", b"escape")

    with pytest.raises(UpdaterError, match="不安全路径"):
        extract_verified_zip(archive, tmp_path / "out", _manifest())

    assert not (tmp_path / "outside.txt").exists()


def test_transactional_install_preserves_user_data_and_keeps_backup(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (install / "config.yaml").write_text("user: true", encoding="utf-8")
    (install / "logs").mkdir()
    (install / "logs" / "debug.log").write_text("evidence", encoding="utf-8")
    (payload / ENTRYPOINT).write_bytes(b"new")
    (payload / "config.yaml").write_text("default: true", encoding="utf-8")
    (payload / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)

    backup = transactional_install(install, payload, "v1.0.0", launch=False)

    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (install / "config.yaml").read_text(encoding="utf-8") == "user: true"
    assert (install / "logs" / "debug.log").read_text(encoding="utf-8") == "evidence"
    assert (install / ".aalc-release.json").is_file()
    assert (backup / ENTRYPOINT).read_bytes() == b"old"


def test_manifest_digest_example_is_sha256():
    digest = hashlib.sha256(b"payload").hexdigest()
    manifest = _manifest(digest=digest)
    assert manifest.sha256 == digest
