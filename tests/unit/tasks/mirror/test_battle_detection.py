import importlib

mirror_module = importlib.import_module("tasks.mirror.mirror")


def test_turn_ocr_reuses_bbox_and_is_rate_limited(monkeypatch) -> None:
    mirror = object.__new__(mirror_module.Mirror)
    mirror._turn_keyword_bbox = None
    mirror._turn_keyword_bbox_loaded = False
    mirror._last_turn_ocr_at = float("-inf")
    loads = []
    ocr_calls = []
    times = iter([10.0, 10.5, 10.75])

    class _Auto:
        def find_text_element(self, keyword, bbox):
            ocr_calls.append((keyword, bbox))
            return "turn"

    monkeypatch.setattr(mirror_module, "auto", _Auto())
    monkeypatch.setattr(mirror_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        mirror_module.ImageUtils,
        "load_image",
        lambda path: loads.append(path) or object(),
    )
    monkeypatch.setattr(mirror_module.ImageUtils, "get_bbox", lambda _image: (1, 2, 3, 4))

    assert mirror._is_turn_visible_by_ocr() is True
    assert mirror._is_turn_visible_by_ocr() is False
    assert mirror._is_turn_visible_by_ocr() is True
    assert loads == ["battle/turn_assets.png"]
    assert ocr_calls == [("turn", (1, 2, 3, 4)), ("turn", (1, 2, 3, 4))]
