import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from types import SimpleNamespace

import numpy as np
import pytest

from module.automation.input_handlers.simulator.mumu_control import MumuControl
from module.automation.screenshot import ScreenShot


class _Future:
    def __init__(self, results):
        self.results = iter(results)
        self.timeouts = []

    def result(self, timeout):
        self.timeouts.append(timeout)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


class _Executor:
    def __init__(self, future):
        self.future = future
        self.submissions = []

    def submit(self, func):
        self.submissions.append(func)
        return self.future


def _make_device():
    device = object.__new__(MumuControl)
    device.connect_id = 1
    device.display_id = 0
    device.width = 2
    device.height = 1
    device._ev_lock = threading.RLock()
    device._screenshot_state_lock = threading.Lock()
    device._pending_screenshot = None
    return device


def test_mumu_screenshot_allows_normal_ipc_latency():
    device = _make_device()
    expected = np.zeros((1, 2, 3), dtype=np.uint8)
    future = _Future([expected])
    executor = _Executor(future)
    device._screenshot_executor = executor

    image = device.screenshot()

    assert image is expected
    assert future.timeouts == [0.5]
    assert len(executor.submissions) == 1
    assert device._pending_screenshot is None


def test_mumu_screenshot_reuses_late_future_instead_of_starting_a_second_capture():
    device = _make_device()
    expected = np.zeros((1, 2, 3), dtype=np.uint8)
    future = _Future([FutureTimeoutError(), expected])
    executor = _Executor(future)
    device._screenshot_executor = executor

    with pytest.raises(TimeoutError, match="等待同一个 IPC 调用完成"):
        device.screenshot(timeout=0.1)

    assert device._pending_screenshot is future
    assert device.screenshot(timeout=0.5) is expected
    assert future.timeouts == [0.1, 0.5]
    assert len(executor.submissions) == 1
    assert device._pending_screenshot is None


def test_concurrent_mumu_screenshots_share_one_pending_future():
    device = _make_device()
    expected = np.zeros((1, 2, 3), dtype=np.uint8)
    release = threading.Event()
    condition = threading.Condition()
    waiters = 0

    class _BlockingFuture:
        def result(self, timeout):
            nonlocal waiters
            with condition:
                waiters += 1
                condition.notify_all()
            assert release.wait(timeout)
            return expected

    executor = _Executor(_BlockingFuture())
    device._screenshot_executor = executor
    start = threading.Barrier(3)

    def capture():
        start.wait()
        return device.screenshot(timeout=1)

    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(capture)
        second = callers.submit(capture)
        start.wait()
        with condition:
            assert condition.wait_for(lambda: waiters == 2, timeout=2)
        release.set()
        assert first.result(timeout=2) is expected
        assert second.result(timeout=2) is expected

    assert len(executor.submissions) == 1
    assert device._pending_screenshot is None


def test_mumu_screenshot_clears_failed_future_for_recovery():
    device = _make_device()
    future = _Future([RuntimeError("capture failed")])
    device._screenshot_executor = _Executor(future)

    with pytest.raises(RuntimeError, match="capture failed"):
        device.screenshot()

    assert device._pending_screenshot is None


def test_capture_display_converts_native_bgra_buffer_to_rgb():
    device = _make_device()
    device.width = 1
    device.height = 1
    lock_state = {"held": False}

    class _TrackingLock:
        def __enter__(self):
            lock_state["held"] = True

        def __exit__(self, _exc_type, _exc_value, _traceback):
            lock_state["held"] = False

    def capture(_connect_id, _display_id, length, _width, _height, pixels):
        assert lock_state["held"]
        assert length == 4
        pixels.contents[0] = 10
        pixels.contents[1] = 20
        pixels.contents[2] = 30
        pixels.contents[3] = 255
        return 0

    device.lib = SimpleNamespace(nemu_capture_display=capture)
    device._ev_lock = _TrackingLock()

    image = device._capture_display()

    assert image.tolist() == [[[30, 20, 10]]]
    assert not lock_state["held"]


def test_mumu_screenshot_keeps_rgb_channel_order(monkeypatch):
    rgb = np.array([[[30, 20, 10]]], dtype=np.uint8)
    connection = SimpleNamespace(screenshot=lambda: rgb)
    monkeypatch.setattr(MumuControl, "connection_device", connection)

    image = ScreenShot.mumu_screenshot(gray=False)

    assert image.getpixel((0, 0)) == (30, 20, 10)
