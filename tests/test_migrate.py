import json

import pytest

from trading_bot.migrate import export_text, import_text
from trading_bot.notify import TelegramNotifier


def test_export_import_roundtrip(tmp_path):
    src, dst = tmp_path / "phone", tmp_path / "pc"
    src.mkdir(); dst.mkdir()
    state = {"date": "2026-10-20", "holdings": {"": {"010140": {"qty": 7, "price": 18400.0}}}}
    (src / "bot_state_api_namuh_real.json").write_text(json.dumps(state))
    (dst / "bot_state_api_namuh_real.json").write_text("{}")
    text = export_text([src / "bot_state_api_namuh_real.json"])
    assert text.startswith("TBSTATE:") and "\n" not in text
    assert import_text(text, dst) == ["bot_state_api_namuh_real.json"]
    assert json.loads((dst / "bot_state_api_namuh_real.json").read_text()) == state
    assert (dst / "bot_state_api_namuh_real.json.bak").exists()


def test_import_rejects_bad_names(tmp_path):
    with pytest.raises(ValueError):
        import_text('TBSTATE:{"files":{"../.env":{}}}', tmp_path)
    with pytest.raises(ValueError):
        import_text("hello", tmp_path)


def test_telegram_conflict_flag():
    class R:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"ok": True, "result": []}

    class S:
        code = 409

        def get(self, url, params, timeout):
            return R(self.code)

    tg = TelegramNotifier("T", "1", session=S())
    tg.get_updates(0)
    assert tg.conflict
    tg.session.code = 200
    tg.get_updates(0)
    assert not tg.conflict
