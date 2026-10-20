from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.ticks import TickWatcher
from tests.test_bot import FixedSignal


def bars(today_close, today_vol, avg_vol=1000.0, prev_close=100.0, with_today=True):
    idx = list(pd.bdate_range("2026-09-01", "2026-10-16"))
    closes = [prev_close] * len(idx)
    vols = [avg_vol] * len(idx)
    if with_today:
        idx.append(pd.Timestamp("2026-10-19"))
        closes.append(today_close)
        vols.append(today_vol)
    c = pd.Series(closes, index=idx)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": vols})


NOW = datetime(2026, 10, 19, 10, 0, tzinfo=KST)  # 장 시작 60분 뒤 = 하루의 60/390


def make(tmp_path, **prices):
    broker = PaperBroker(prices=prices)
    return TickWatcher(broker, list(prices), data_dir=tmp_path / "ticks"), broker


def test_records_and_detects_spike_once(tmp_path):
    # 평소 같은 시간대 기대 거래량 = 1000 × 60/390 ≈ 154 → 1000 이면 6.5배, +5%
    w, _ = make(tmp_path, A=bars(105.0, 1000.0), B=bars(101.0, 100.0))
    spikes = w.poll(NOW)
    assert [s.symbol for s in spikes] == ["A"]
    assert spikes[0].change > 0.049 and spikes[0].vol_ratio > 6
    assert w.poll(NOW) == []  # 같은 날 두 번 알리지 않음
    rows = (tmp_path / "ticks" / "2026-10-19.csv").read_text().splitlines()
    assert rows[0] == "time,symbol,price,volume" and len(rows) == 5
    assert rows[1].endswith(",A,105,1000")


def test_volume_without_price_move_is_not_spike(tmp_path):
    w, _ = make(tmp_path, A=bars(100.5, 5000.0))
    assert w.poll(NOW) == []


def test_skips_when_no_today_bar(tmp_path):
    w, _ = make(tmp_path, A=bars(105.0, 1000.0, with_today=False))
    assert w.poll(NOW) == []
    assert (tmp_path / "ticks" / "2026-10-19.csv").read_text().splitlines() == ["time,symbol,price,volume"]


class Sink:
    enabled = False

    def __init__(self):
        self.sent = []

    def send(self, text, html=False):
        self.sent.append(text)


def test_bot_tick_notifies_but_does_not_trade(tmp_path):
    broker = PaperBroker(cash=1_000_000, prices={"A": bars(105.0, 1000.0)})
    w = TickWatcher(broker, ["A"], data_dir=tmp_path / "ticks")
    sink = Sink()
    bot = TradingBot(broker, FixedSignal(0), BotConfig(symbols=["A"]), state_path=tmp_path / "s.json",
                     notifier=sink, watcher=w)
    bot.tick(NOW)
    assert any("급등 감지" in t for t in sink.sent)
    assert broker.positions == {} and bot.state.orders == 0
    bot.tick(datetime(2026, 10, 18, 10, 0, tzinfo=KST))  # 일요일: 아무것도 안 함
    assert "급등" in bot.describe() and "60초" in bot.describe()
