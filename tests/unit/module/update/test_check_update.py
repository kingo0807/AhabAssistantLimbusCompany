import ssl
from types import SimpleNamespace

import pytest

from module.update import check_update as update_module


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True

    def json(self):
        return self.payload


def test_update_session_mounts_windows_system_trust(monkeypatch):
    context = object()
    mounted = {}

    class _Session:
        def mount(self, prefix, adapter):
            mounted[prefix] = adapter

    monkeypatch.setattr(update_module.requests, "Session", _Session)
    monkeypatch.setattr(update_module.os, "name", "nt")
    monkeypatch.setattr(update_module.ssl, "create_default_context", lambda: context)

    session = update_module.create_update_http_session()

    assert isinstance(session, _Session)
    assert mounted["https://"].ssl_context is context


def test_update_session_keeps_default_adapter_outside_windows(monkeypatch):
    mounted = []

    class _Session:
        def mount(self, prefix, adapter):
            mounted.append((prefix, adapter))

    monkeypatch.setattr(update_module.requests, "Session", _Session)
    monkeypatch.setattr(update_module.os, "name", "posix")

    assert isinstance(update_module.create_update_http_session(), _Session)
    assert mounted == []


def test_windows_adapter_passes_system_context_to_direct_and_proxy_pools(monkeypatch):
    context = ssl.create_default_context()
    adapter = update_module.WindowsTrustHTTPAdapter(context)
    direct = {}
    proxy = {}

    monkeypatch.setattr(
        update_module.HTTPAdapter,
        "init_poolmanager",
        lambda self, connections, maxsize, block=False, **kwargs: direct.update(kwargs),
    )
    monkeypatch.setattr(
        update_module.HTTPAdapter,
        "proxy_manager_for",
        lambda self, proxy_url, **kwargs: proxy.update(kwargs),
    )

    adapter.init_poolmanager(10, 10)
    adapter.proxy_manager_for("https://proxy.example")

    assert direct["ssl_context"] is context
    assert proxy["ssl_context"] is context
    assert proxy["proxy_ssl_context"] is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize(
    ("prerelease", "path", "payload", "expected"),
    [
        (False, "/releases/latest", {"tag_name": "v1.2.3"}, {"tag_name": "v1.2.3"}),
        (True, "/releases", [{"tag_name": "v1.3.0-beta"}], {"tag_name": "v1.3.0-beta"}),
    ],
)
def test_github_update_check_uses_system_trust_session(monkeypatch, prerelease, path, payload, expected):
    calls = []
    response = _Response(payload)

    class _Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    thread = update_module.UpdateThread.__new__(update_module.UpdateThread)
    thread.user = "kingo0807"
    thread.repo = "AhabAssistantLimbusCompany"
    thread.http_session = _Session()
    monkeypatch.setattr(update_module.cfg, "update_prerelease_enable", prerelease)
    monkeypatch.setattr(update_module.cfg, "useragent", {"User-Agent": "AALC-test"})

    assert thread.check_update_info_github() == expected
    assert calls == [
        (
            f"https://api.github.com/repos/kingo0807/AhabAssistantLimbusCompany{path}",
            {"timeout": 10, "headers": {"User-Agent": "AALC-test"}},
        )
    ]
    assert response.raise_for_status_called is True


def test_update_download_uses_system_trust_session(monkeypatch):
    calls = []
    emitted = []
    created_directories = []
    opened_files = []
    downloaded = bytearray()

    class _DownloadResponse:
        headers = {"content-length": "7"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 1024
            return iter((b"payload",))

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return _DownloadResponse()

    class _File:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, chunk):
            downloaded.extend(chunk)

    def fake_open(path, mode):
        opened_files.append((path, mode))
        return _File()

    monkeypatch.setattr(update_module, "create_update_http_session", _Session)
    monkeypatch.setattr(
        update_module.os,
        "makedirs",
        lambda path, **kwargs: created_directories.append((path, kwargs)),
    )
    monkeypatch.setattr(update_module, "open", fake_open, raising=False)
    monkeypatch.setattr(
        update_module,
        "mediator",
        SimpleNamespace(
            update_progress=SimpleNamespace(emit=lambda value: emitted.append(("progress", value))),
            download_complete=SimpleNamespace(emit=lambda value: emitted.append(("complete", value))),
        ),
    )

    update_module.update("https://github.example/AALC-Optimized-win64.zip")

    assert calls == [
        (
            "https://github.example/AALC-Optimized-win64.zip",
            {"stream": True, "timeout": 10},
        )
    ]
    assert created_directories == [("update_temp", {"exist_ok": True})]
    assert opened_files == [(update_module.os.path.join("update_temp", "AALC.zip"), "wb")]
    assert downloaded == b"payload"
    assert emitted == [("progress", 100), ("complete", "AALC.zip")]
