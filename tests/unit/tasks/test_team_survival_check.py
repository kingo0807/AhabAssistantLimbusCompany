import importlib


team_formation = importlib.import_module("tasks.teams.team_formation")


class _TeamCountAuto:
    def __init__(self, texts):
        self.texts = texts

    def get_text_positions_from_screenshot(self):
        return {text: [0, 0] for text in self.texts}


def test_parse_available_sinner_count_accepts_full_twelve_person_team():
    parse = team_formation._parse_available_sinner_count

    assert parse(("参战人数12/12",)) == 12
    assert parse(("参战人数 9／12",)) == 9
    assert parse(("其他进度3/5", "参战人数8/12")) == 8


def test_check_team_only_rejects_confirmed_count_below_five(monkeypatch):
    monkeypatch.setattr(team_formation, "auto", _TeamCountAuto(("参战人数12/12",)))
    assert team_formation.check_team() is True

    monkeypatch.setattr(team_formation, "auto", _TeamCountAuto(("参战人数4/12",)))
    assert team_formation.check_team() is False


def test_check_team_fails_open_when_ocr_is_fragmented(monkeypatch):
    monkeypatch.setattr(team_formation, "auto", _TeamCountAuto(("战人数", "12", "开始战斗")))

    assert team_formation.check_team() is None
