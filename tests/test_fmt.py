from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, TradingBot
from trading_bot.broker.base import Position
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig
from trading_bot.fmt import arrow, esc, kdate, strip_tags
from trading_bot.notify import TelegramNotifier


class Raw:
    """태그를 지우지 않고 그대로 기록 (HTML 검증용)."""

    conflict = False

    def __init__(self):
        self.sent = []

    def send(self, text, html=False):
        self.sent.append((text, html))

    def poll_commands(self, timeout=0):
        return []


class Fixed:
    name, warmup = "fixed", 1

    def __init__(self, v):
        self.v = v

    def signals(self, df):
        return pd.Series(self.v, index=df.index)


def prices(last=100.0):
    idx = pd.bdate_range("2026-10-01", "2026-10-19")
    c = pd.Series(100.0, index=idx)
    c.iloc[-1] = last
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def test_helpers():
    assert esc("S&T모티브 <x>") == "S&amp;T모티브 &lt;x&gt;"
    assert strip_tags("<b>a &amp; b</b>") == "a & b"
    assert (arrow(1), arrow(-1), arrow(0)) == ("🔺", "🔻", "▫️")
    assert kdate(datetime(2026, 10, 21)) == "10/21(수)"


def test_messages_are_html_and_names_escaped(tmp_path):
    broker = PaperBroker(cash=1_000_000, prices={"064960": prices()}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["064960"], risk=RiskConfig(max_position_pct=0.5, max_daily_loss_pct=0), commission=0)
    raw = Raw()
    bot = TradingBot(broker, Fixed(1), cfg, state_path=tmp_path / "s.json", notifier=raw, label="실전")
    bot.names["064960"] = "S&T모티브"
    bot.step(datetime(2026, 10, 19, 9, 10, tzinfo=KST))
    assert all(html for _, html in raw.sent)
    buy = next(t for t, _ in raw.sent if "매수 주문" in t)
    assert "S&amp;T모티브(064960)" in buy and "<b>[실전]</b>" in buy and "<b>매수 주문</b>" in buy
    assert "S&T" not in buy  # 이스케이프 안 된 & 가 없어야 텔레그램이 거부하지 않음
    # 일반 문자열 알림(오류 등)은 통째로 이스케이프
    bot.notify_error("HTTP 400 <html>bad & worse</html>")
    text, html = raw.sent[-1]
    assert html and "&lt;html&gt;bad &amp; worse" in text


def test_export_payload_sent_plain(tmp_path):
    broker = PaperBroker(cash=1_000_000, prices={"A": prices()}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)
    raw = Raw()
    bot = TradingBot(broker, Fixed(1), cfg, state_path=tmp_path / "s.json", notifier=raw)
    bot.handle_command("/export")
    payload, html = raw.sent[-1]
    assert payload.startswith("TBSTATE:") and html is False


def test_html_rejected_falls_back_to_plain():
    class R:
        def __init__(self, ok, status):
            self.ok, self.status_code = ok, status

    class S:
        def __init__(self):
            self.calls = []

        def post(self, url, json, timeout):
            self.calls.append(json)
            return R(False, 400) if "parse_mode" in json else R(True, 200)

    tg = TelegramNotifier("T", "1", session=S())
    tg.send("<b>굵게</b> &amp; 보통", html=True)
    assert tg.session.calls[0]["parse_mode"] == "HTML"
    assert tg.session.calls[1] == {"chat_id": "1", "text": "굵게 & 보통"}  # 서식 제거 후 재전송


def test_plain_send_has_no_parse_mode():
    class S:
        def __init__(self):
            self.calls = []

        def post(self, url, json, timeout):
            self.calls.append(json)

    tg = TelegramNotifier("T", "1", session=S())
    tg.send("TBSTATE:{}")
    assert tg.session.calls == [{"chat_id": "1", "text": "TBSTATE:{}"}]
