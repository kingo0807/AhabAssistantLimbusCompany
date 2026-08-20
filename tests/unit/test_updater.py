import hashlib
import urllib.error
import zipfile
from argparse import Namespace
from pathlib import Path

import psutil
import pytest

from updater import (
    ENTRYPOINT,
    LATEST_MANIFEST_URL,
    RELEASE_DOWNLOAD_BASE,
    StandaloneUpdater,
    UpdateManifest,
    UpdateNetworkError,
    UpdaterError,
    extract_verified_zip,
    fetch_json,
    find_local_archive,
    inspect_local_archive,
    launch_entrypoint,
    parse_release,
    relaunch_worker,
    resolve_install_dir,
    stop_target_processes,
    transactional_install,
    wait_for_parent_exit,
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


def _manifest_payload(**overrides):
    payload = {
        "schema_version": 1,
        "version": "v1.0.0",
        "asset": "AALC-Optimized-win64.zip",
        "sha256": "0" * 64,
        "size": 123,
        "entrypoint": ENTRYPOINT,
        "archive_root": "AALC",
    }
    payload.update(overrides)
    return payload


def test_release_api_network_error_falls_back_to_public_manifest(tmp_path, monkeypatch, capsys):
    api_url = "https://api.example.invalid/releases/latest"
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == api_url:
            raise UpdateNetworkError("HTTP Error 403: rate limit exceeded")
        assert url == LATEST_MANIFEST_URL
        return _manifest_payload(version="v1.0.0+fast")

    monkeypatch.setattr("updater.fetch_json", fake_fetch)

    tag, assets, manifest = StandaloneUpdater(tmp_path, api_url)._load_release()

    assert calls == [api_url, LATEST_MANIFEST_URL]
    assert tag == manifest.version == "v1.0.0+fast"
    assert assets[manifest.asset].url == (
        f"{RELEASE_DOWNLOAD_BASE}/v1.0.0%2Bfast/AALC-Optimized-win64.zip"
    )
    assert "改用公开 Release 直链" in capsys.readouterr().out


def test_fetch_json_classifies_http_403_as_network_error(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.example.invalid/releases/latest",
        403,
        "rate limit exceeded",
        None,
        None,
    )
    monkeypatch.setattr("updater._request", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(UpdateNetworkError, match="403.*rate limit exceeded"):
        fetch_json("https://api.example.invalid/releases/latest")


def test_release_api_success_does_not_use_fallback(tmp_path, monkeypatch):
    api_url = "https://api.example.invalid/releases/latest"
    manifest_url = "https://downloads.example.invalid/update-manifest.json"
    package_url = "https://downloads.example.invalid/AALC-Optimized-win64.zip"
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == api_url:
            return {
                "tag_name": "v1.0.0",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": "update-manifest.json", "browser_download_url": manifest_url, "size": 100},
                    {
                        "name": "AALC-Optimized-win64.zip",
                        "browser_download_url": package_url,
                        "size": 123,
                    },
                ],
            }
        assert url == manifest_url
        return _manifest_payload()

    monkeypatch.setattr("updater.fetch_json", fake_fetch)

    tag, assets, manifest = StandaloneUpdater(tmp_path, api_url)._load_release()

    assert calls == [api_url, manifest_url]
    assert tag == manifest.version == "v1.0.0"
    assert assets[manifest.asset].url == package_url


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": "../v1.0.0"}, "版本号"),
        ({"asset": "../payload.zip"}, "ZIP 资产名"),
        ({"sha256": "not-a-sha256"}, "SHA-256"),
        ({"size": 0}, "文件大小"),
    ],
)
def test_public_manifest_fallback_rejects_unsafe_or_invalid_fields(
    tmp_path, monkeypatch, overrides, message
):
    api_url = "https://api.example.invalid/releases/latest"

    def fake_fetch(url):
        if url == api_url:
            raise UpdateNetworkError("HTTP Error 403: rate limit exceeded")
        assert url == LATEST_MANIFEST_URL
        return _manifest_payload(**overrides)

    monkeypatch.setattr("updater.fetch_json", fake_fetch)

    with pytest.raises(UpdaterError, match=message):
        StandaloneUpdater(tmp_path, api_url)._load_release()


def test_invalid_api_response_is_not_hidden_by_fallback(tmp_path, monkeypatch):
    api_url = "https://api.example.invalid/releases/latest"
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return {"unexpected": True}

    monkeypatch.setattr("updater.fetch_json", fake_fetch)

    with pytest.raises(UpdaterError, match="响应格式无效|缺少版本号"):
        StandaloneUpdater(tmp_path, api_url)._load_release()

    assert calls == [api_url]


def test_extract_verified_zip_rejects_path_escape(tmp_path):
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AALC/AALC.exe", b"new")
        package.writestr("AALC/../../outside.txt", b"escape")

    with pytest.raises(UpdaterError, match="不安全路径"):
        extract_verified_zip(archive, tmp_path / "out", _manifest())

    assert not (tmp_path / "outside.txt").exists()


def test_inspect_local_archive_reads_version_without_network(tmp_path):
    archive = tmp_path / "AALC-Optimized-win64.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AALC/AALC.exe", b"new")
        package.writestr("AALC/assets/config/version.txt", "v1.2.3-local")

    manifest = inspect_local_archive(archive)

    assert manifest.version == "v1.2.3-local"
    assert manifest.asset == archive.name
    assert manifest.size == archive.stat().st_size
    assert manifest.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_find_local_archive_requires_fixed_name_in_selected_folder(tmp_path):
    install = tmp_path / "AALC"
    install.mkdir()
    expected = install / "AALC-Optimized-win64.zip"

    with pytest.raises(UpdaterError, match="应放置在"):
        find_local_archive(install)

    expected.write_bytes(b"zip")
    assert find_local_archive(install) == expected.resolve()


def test_local_archive_update_never_reads_network(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    install.mkdir()
    (install / ENTRYPOINT).write_bytes(b"old")
    archive = tmp_path / "AALC-Optimized-win64.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("AALC/AALC.exe", b"new")
        package.writestr("AALC/assets/config/version.txt", "v1.2.3-offline")

    monkeypatch.setattr(
        "updater.fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应访问网络")),
    )
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)

    backup = StandaloneUpdater(install).run(source_archive=archive, launch=False)

    assert backup is not None
    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (install / ".aalc-release.json").read_text(encoding="utf-8").find("v1.2.3-offline") >= 0


def test_transactional_install_preserves_user_data_and_keeps_backup(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (install / "config.yaml").write_text("user: true", encoding="utf-8")
    (install / "logs").mkdir()
    (install / "logs" / "debug.log").write_text("evidence", encoding="utf-8")
    (install / "issue_recordings").mkdir()
    (install / "issue_recordings" / "failure.mp4").write_bytes(b"recording")
    (install / "上传AALC日志-v1.3.1.exe").write_bytes(b"uploader")
    (payload / ENTRYPOINT).write_bytes(b"new")
    (payload / "config.yaml").write_text("default: true", encoding="utf-8")
    (payload / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)

    backup = transactional_install(install, payload, "v1.0.0", launch=False)

    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (install / "config.yaml").read_text(encoding="utf-8") == "user: true"
    assert (install / "logs" / "debug.log").read_text(encoding="utf-8") == "evidence"
    assert (install / "issue_recordings" / "failure.mp4").read_bytes() == b"recording"
    assert (install / "上传AALC日志-v1.3.1.exe").read_bytes() == b"uploader"
    assert (install / ".aalc-release.json").is_file()
    assert (backup / ENTRYPOINT).read_bytes() == b"old"


def test_launch_entrypoint_requests_uac_when_admin_is_required(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    install.mkdir()
    executable = install / ENTRYPOINT
    executable.write_bytes(b"app")
    shell_execute_args = {}

    class ElevationRequired(OSError):
        winerror = 740

    def fake_shell_execute(*args):
        shell_execute_args["args"] = args
        return 42

    monkeypatch.setattr("updater.subprocess.Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(ElevationRequired()))
    monkeypatch.setattr("updater.ctypes.windll.shell32.ShellExecuteW", fake_shell_execute)

    assert launch_entrypoint(install) is True
    assert shell_execute_args["args"][1] == "runas"
    assert shell_execute_args["args"][2] == str(executable)
    assert shell_execute_args["args"][4] == str(install)


def test_launch_entrypoint_keeps_successful_update_when_uac_is_cancelled(tmp_path, monkeypatch, capsys):
    install = tmp_path / "AALC"
    install.mkdir()
    (install / ENTRYPOINT).write_bytes(b"app")

    class ElevationRequired(OSError):
        winerror = 740

    monkeypatch.setattr("updater.subprocess.Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(ElevationRequired()))
    monkeypatch.setattr("updater.ctypes.windll.shell32.ShellExecuteW", lambda *_args: 5)

    assert launch_entrypoint(install) is False
    assert "更新已完成，但未自动启动" in capsys.readouterr().out


def test_transactional_install_does_not_fail_when_auto_launch_fails(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (payload / ENTRYPOINT).write_bytes(b"new")
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)
    monkeypatch.setattr("updater.launch_entrypoint", lambda _install: False)

    backup = transactional_install(install, payload, "v1.0.1")

    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (backup / ENTRYPOINT).read_bytes() == b"old"


def test_manifest_digest_example_is_sha256():
    digest = hashlib.sha256(b"payload").hexdigest()
    manifest = _manifest(digest=digest)
    assert manifest.sha256 == digest


def test_double_click_target_is_exactly_updater_folder(tmp_path):
    first = tmp_path / "AALC-one"
    second = tmp_path / "AALC-two"
    first.mkdir()
    second.mkdir()
    executable = first / "AALC-Update.exe"
    executable.touch()

    args = Namespace(worker=False, install_dir=None)

    assert resolve_install_dir(args, executable) == first.resolve()
    assert resolve_install_dir(args, executable) != second.resolve()


def test_external_install_dir_override_is_rejected(tmp_path):
    executable = tmp_path / "AALC-one" / "AALC-Update.exe"
    other = tmp_path / "AALC-two"

    with pytest.raises(UpdaterError, match="不能从命令行指定"):
        resolve_install_dir(Namespace(worker=False, install_dir=other), executable)


def test_worker_keeps_original_folder_after_relaunch(tmp_path):
    install = tmp_path / "AALC-one"
    worker_executable = tmp_path / "system-temp" / "AALC 更新程序.exe"

    assert resolve_install_dir(Namespace(worker=True, install_dir=install), worker_executable) == install.resolve()


def test_relaunch_worker_passes_parent_pid_and_original_folder(tmp_path, monkeypatch):
    install = tmp_path / "AALC-one"
    install.mkdir()
    executable = install / "AALC-Update.exe"
    executable.write_bytes(b"updater")
    workspace = tmp_path / "worker"
    workspace.mkdir()
    started = {}

    monkeypatch.setattr("updater.sys.frozen", True, raising=False)
    monkeypatch.setattr("updater._executable_path", lambda: executable)
    monkeypatch.setattr("updater.tempfile.mkdtemp", lambda prefix: str(workspace))
    monkeypatch.setattr("updater.os.getpid", lambda: 4321)

    def fake_popen(command, **kwargs):
        started["command"] = command
        started["kwargs"] = kwargs

    monkeypatch.setattr("updater.subprocess.Popen", fake_popen)
    args = Namespace(
        api_url="https://example.invalid/releases/latest",
        check_only=False,
        no_launch=False,
        force=False,
    )

    relaunch_worker(args, install, None)

    command = started["command"]
    assert command[command.index("--parent-pid") + 1] == "4321"
    assert command[command.index("--install-dir") + 1] == str(install)
    assert started["kwargs"]["cwd"] == workspace


def test_wait_for_parent_exit_waits_for_requested_process(monkeypatch):
    waited = {}

    class FakeParent:
        def wait(self, timeout):
            waited["timeout"] = timeout

    def fake_process(pid):
        waited["pid"] = pid
        return FakeParent()

    monkeypatch.setattr("updater.psutil.Process", fake_process)

    wait_for_parent_exit(4321, timeout=3.0)

    assert waited == {"pid": 4321, "timeout": 3.0}


def test_wait_for_parent_exit_rejects_timeout(monkeypatch):
    class SlowParent:
        def wait(self, timeout):
            raise psutil.TimeoutExpired(timeout, pid=4321)

    monkeypatch.setattr("updater.psutil.Process", lambda _pid: SlowParent())

    with pytest.raises(UpdaterError, match="旧更新程序未能退出"):
        wait_for_parent_exit(4321, timeout=0.01)


def test_transactional_install_does_not_touch_second_aalc(tmp_path, monkeypatch):
    first = tmp_path / "AALC-one"
    second = tmp_path / "AALC-two"
    payload = tmp_path / "payload" / "AALC"
    for install, marker in ((first, b"first-old"), (second, b"second-untouched")):
        install.mkdir()
        (install / ENTRYPOINT).write_bytes(marker)
        (install / "config.yaml").write_text(marker.decode(), encoding="utf-8")
    payload.mkdir(parents=True)
    (payload / ENTRYPOINT).write_bytes(b"first-new")
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)

    transactional_install(first, payload, "v1.0.0", launch=False)

    assert (first / ENTRYPOINT).read_bytes() == b"first-new"
    assert (second / ENTRYPOINT).read_bytes() == b"second-untouched"
    assert (second / "config.yaml").read_text(encoding="utf-8") == "second-untouched"
    assert not (second / ".aalc-release.json").exists()


def test_stop_processes_only_targets_selected_folder(tmp_path, monkeypatch):
    first = tmp_path / "AALC-one"
    second = tmp_path / "AALC-two"

    class FakeProcess:
        def __init__(self, pid, executable):
            self.info = {"pid": pid, "name": ENTRYPOINT, "exe": str(executable)}
            self.terminated = False

        def terminate(self):
            self.terminated = True

    selected = FakeProcess(1001, first / ENTRYPOINT)
    untouched = FakeProcess(1002, second / ENTRYPOINT)
    monkeypatch.setattr("updater.os.getpid", lambda: 999)
    monkeypatch.setattr("updater.psutil.process_iter", lambda _attrs: [selected, untouched])
    monkeypatch.setattr("updater.psutil.wait_procs", lambda processes, timeout: (processes, []))

    stop_target_processes(first)

    assert selected.terminated is True
    assert untouched.terminated is False


def test_stop_processes_closes_non_entrypoint_executable_inside_selected_folder(tmp_path, monkeypatch):
    install = tmp_path / "AALC"

    class FakeProcess:
        def __init__(self, pid, name, executable, children=()):
            self.info = {"pid": pid, "name": name, "exe": str(executable)}
            self.pid = pid
            self._children = list(children)
            self.terminated = False

        def children(self, recursive=False):
            assert recursive is True
            return self._children

        def terminate(self):
            self.terminated = True

    helper = FakeProcess(1003, "ping.exe", Path("C:/Windows/System32/ping.exe"))
    adb = FakeProcess(1001, "adb.exe", install / "_internal" / "platform-tools" / "adb.exe", children=[helper])
    external = FakeProcess(1002, "adb.exe", tmp_path / "other" / "adb.exe")
    monkeypatch.setattr("updater.os.getpid", lambda: 999)
    monkeypatch.setattr("updater.psutil.process_iter", lambda _attrs: [adb, external])
    monkeypatch.setattr("updater.psutil.wait_procs", lambda processes, timeout: (processes, []))

    stop_target_processes(install)

    assert adb.terminated is True
    assert helper.terminated is True
    assert external.terminated is False


def test_stop_processes_closes_external_descendant_spawned_by_aalc(tmp_path, monkeypatch):
    install = tmp_path / "AALC"

    class FakeProcess:
        def __init__(self, pid, name, executable, children=()):
            self.info = {"pid": pid, "name": name, "exe": str(executable)}
            self.pid = pid
            self._children = list(children)
            self.terminated = False

        def children(self, recursive=False):
            assert recursive is True
            return self._children

        def terminate(self):
            self.terminated = True

    terminal = FakeProcess(1002, "cmd.exe", Path("C:/Windows/System32/cmd.exe"))
    aalc = FakeProcess(1001, ENTRYPOINT, install / ENTRYPOINT, children=[terminal])
    monkeypatch.setattr("updater.os.getpid", lambda: 999)
    monkeypatch.setattr("updater.psutil.process_iter", lambda _attrs: [aalc])
    monkeypatch.setattr("updater.psutil.wait_procs", lambda processes, timeout: (processes, []))

    stop_target_processes(install)

    assert aalc.terminated is True
    assert terminal.terminated is True


def test_stop_processes_reports_elevated_process_when_wait_access_is_denied(tmp_path, monkeypatch):
    install = tmp_path / "AALC"

    class ElevatedProcess:
        info = {"pid": 1001, "name": ENTRYPOINT, "exe": str(install / ENTRYPOINT)}
        pid = 1001

        def children(self, recursive=False):
            return []

        def terminate(self):
            raise psutil.AccessDenied(pid=self.pid)

        def kill(self):
            raise psutil.AccessDenied(pid=self.pid)

        def is_running(self):
            return True

        def name(self):
            return ENTRYPOINT

    process = ElevatedProcess()
    monkeypatch.setattr("updater.os.getpid", lambda: 999)
    monkeypatch.setattr("updater.psutil.process_iter", lambda _attrs: [process])
    monkeypatch.setattr(
        "updater.psutil.wait_procs",
        lambda _processes, timeout: (_ for _ in ()).throw(psutil.AccessDenied(pid=process.pid)),
    )

    with pytest.raises(UpdaterError, match=r"AALC\.exe \(PID 1001\)"):
        stop_target_processes(install)


def test_transactional_install_retries_transient_windows_directory_lock(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (payload / ENTRYPOINT).write_bytes(b"new")
    original_rename = Path.rename
    attempts = 0
    sleeps = []

    def flaky_rename(path, destination):
        nonlocal attempts
        if path == install and attempts < 2:
            attempts += 1
            raise PermissionError(5, "directory is still locked")
        return original_rename(path, destination)

    monkeypatch.setattr("updater.Path.rename", flaky_rename)
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)
    monkeypatch.setattr("updater.time.sleep", sleeps.append)

    backup = transactional_install(install, payload, "v1.0.2", launch=False)

    assert attempts == 2
    assert sleeps == [0.5, 1.0]
    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (backup / ENTRYPOINT).read_bytes() == b"old"


def test_transactional_install_falls_back_to_in_place_mirror_after_permanent_lock(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (install / "stale.txt").write_text("old", encoding="utf-8")
    (install / "config.yaml").write_text("user: true", encoding="utf-8")
    (payload / ENTRYPOINT).write_bytes(b"new")
    (payload / "new.txt").write_text("new", encoding="utf-8")
    (payload / "config.yaml").write_text("default: true", encoding="utf-8")
    original_rename = Path.rename

    def locked_rename(path, destination):
        if path == install:
            raise PermissionError(5, "directory remains locked")
        return original_rename(path, destination)

    monkeypatch.setattr("updater.Path.rename", locked_rename)
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)
    monkeypatch.setattr("updater.time.sleep", lambda _delay: None)
    monkeypatch.setattr("updater._blocking_process_summary", lambda _root: "cmd.exe (PID 42：当前工作目录位于该目录)")

    backup = transactional_install(install, payload, "v1.0.2", launch=False)

    assert (install / ENTRYPOINT).read_bytes() == b"new"
    assert (install / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (install / "stale.txt").exists()
    assert (install / "config.yaml").read_text(encoding="utf-8") == "user: true"
    assert (backup / ENTRYPOINT).read_bytes() == b"old"
    assert (backup / "stale.txt").read_text(encoding="utf-8") == "old"


def test_in_place_mirror_rolls_back_partial_update(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (install / "stale.txt").write_text("old", encoding="utf-8")
    (payload / ENTRYPOINT).write_bytes(b"new")
    original_rename = Path.rename
    updater_module = __import__("updater")
    real_mirror = updater_module._mirror_tree
    mirror_calls = 0

    def locked_rename(path, destination):
        if path == install:
            raise PermissionError(5, "directory remains locked")
        return original_rename(path, destination)

    def fail_first_mirror(source, destination):
        nonlocal mirror_calls
        mirror_calls += 1
        if mirror_calls == 1:
            (destination / ENTRYPOINT).write_bytes(b"partial")
            (destination / "stale.txt").unlink()
            raise OSError(32, "simulated file lock")
        return real_mirror(source, destination)

    monkeypatch.setattr("updater.Path.rename", locked_rename)
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)
    monkeypatch.setattr("updater.time.sleep", lambda _delay: None)
    monkeypatch.setattr("updater._mirror_tree", fail_first_mirror)

    with pytest.raises(UpdaterError, match="已从完整备份恢复旧版本"):
        transactional_install(install, payload, "v1.0.2", launch=False)

    assert (install / ENTRYPOINT).read_bytes() == b"old"
    assert (install / "stale.txt").read_text(encoding="utf-8") == "old"


def test_in_place_mirror_writes_release_marker_last(tmp_path, monkeypatch):
    updater_module = __import__("updater")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ENTRYPOINT).write_bytes(b"new")
    (source / ".aalc-release.json").write_text('{"version":"new"}', encoding="utf-8")
    (destination / ENTRYPOINT).write_bytes(b"old")
    writes = []
    real_replace = updater_module.os.replace

    def record_replace(source_file, target):
        writes.append(Path(target).name)
        return real_replace(source_file, target)

    monkeypatch.setattr("updater.os.replace", record_replace)

    updater_module._mirror_tree(source, destination)

    assert writes[-1] == ".aalc-release.json"
    assert (destination / ENTRYPOINT).read_bytes() == b"new"


def test_transactional_install_restores_backup_when_staging_switch_fails(tmp_path, monkeypatch):
    install = tmp_path / "AALC"
    payload = tmp_path / "payload" / "AALC"
    install.mkdir()
    payload.mkdir(parents=True)
    (install / ENTRYPOINT).write_bytes(b"old")
    (payload / ENTRYPOINT).write_bytes(b"new")
    original_rename = Path.rename

    def fail_staging_switch(path, destination):
        if path.name.startswith(".AALC.update-staging-") and destination == install:
            raise OSError(22, "simulated non-retryable staging failure")
        return original_rename(path, destination)

    monkeypatch.setattr("updater.Path.rename", fail_staging_switch)
    monkeypatch.setattr("updater.stop_target_processes", lambda _install: None)

    with pytest.raises(UpdaterError, match="已自动恢复旧版本"):
        transactional_install(install, payload, "v1.0.2", launch=False)

    assert (install / ENTRYPOINT).read_bytes() == b"old"
    assert not list(tmp_path.glob("AALC.backup-*"))
