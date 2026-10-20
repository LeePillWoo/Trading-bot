from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, Sleeve, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig
from trading_bot.fmt import strip_tags
from trading_bot.notify import Notifier
from trading_bot.strategy import DualMomentumStrategy
from trading_bot.universe import RECORD, load_master, parse_master, top_kospi


def record(code, name, cap, market="1", kind="0", managed="N", halted="N", warn="N", alert="0", liq="N"):
    rec = bytearray(b" " * RECORD)
    rec[-1] = 0x0A

    def put(start, value):
        data = value.encode("cp949")
        rec[start:start + len(data)] = data

    put(0, code); put(6, market); put(7, name); put(160, managed); put(161, halted); put(162, warn)
    put(165, kind); put(174, f"{cap:>12}"); put(188, alert); put(189, liq)
    return bytes(rec)


MASTER = b"".join([
    record("005930", "*삼성전자", 5000000),
    record("005935", "*삼성전자우", 600000),          # 우선주 제외
    record("000660", "*SK하이닉스", 3000000),
    record("069500", "KODEX 200", 900000, kind="8"),  # ETF 제외
    record("247540", "#에코프로비엠", 200000, market="4"),  # 코스닥 제외
    record("111110", "관리종목", 800000, managed="Y"),
    record("222220", "투자경고", 700000, alert="2"),
    record("005380", "*현대차", 500000),
    record("035420", "NAVER", 400000),
])


def test_parse_and_filter():
    rows = parse_master(MASTER)
    assert len(rows) == 9 and rows[0].name == "삼성전자" and rows[0].cap == 5000000
    top = top_kospi(rows, 3)
    assert [x.code for x in top] == ["005930", "000660", "005380"]


def test_load_master_caches(tmp_path):
    calls = []

    class S:
        def get(self, url, timeout):
            calls.append(url)

            class R:
                content = MASTER

                def raise_for_status(self):
                    pass
            return R()

    load_master(cache_dir=tmp_path, session=S())
    load_master(cache_dir=tmp_path, session=S())
    assert len(calls) == 1 and calls[0] == "https://www.nhplug.com/instruments/m_new_stock.mst"


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text, **kw):
        self.messages.append(strip_tags(text))


def test_bot_refreshes_universe_and_sells_dropped(tmp_path):
    idx = pd.bdate_range("2026-08-01", "2026-11-02")
    mk = lambda step: (lambda c: pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}))(
        pd.Series([100 + step * i for i in range(len(idx))], index=idx, dtype=float))
    broker = PaperBroker(cash=1_000_000, prices={"A": mk(1), "B": mk(2), "C": mk(3)},
                         commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(stop_loss_pct=0, max_daily_loss_pct=0), commission=0)
    universe = {"now": (["A", "B"], {"A": "에이", "B": "비"})}
    sleeve = Sleeve(DualMomentumStrategy(lookback=10, top_n=1, rf=0), ["A"], 1.0, "장기", universe_top=2)
    rec = Recorder()
    bot = TradingBot(broker, [sleeve], cfg, state_path=tmp_path / "s.json", notifier=rec,
                     universe_loader=lambda n: universe["now"])
    bot.step(datetime(2026, 10, 1, 9, 10, tzinfo=KST))
    assert set(bot.state.holdings["장기"]) == {"B"}  # A, B 중 B 가 더 많이 오름
    assert any("후보 갱신" in m and "비(B)" in m for m in rec.messages)
    # 다음 달: 후보가 A, C 로 바뀜 → 빠진 B 는 매도, C 매수
    universe["now"] = (["A", "C"], {"A": "에이", "C": "씨"})
    bot.state.rebalanced = False
    bot.step(datetime(2026, 11, 2, 9, 10, tzinfo=KST))
    assert set(bot.state.holdings["장기"]) == {"C"} and "B" not in broker.positions
    # 재시작해도 마지막 후보 유지
    bot2 = TradingBot(broker, [Sleeve(DualMomentumStrategy(), ["A"], 1.0, "장기", universe_top=2)], cfg,
                      state_path=tmp_path / "s.json", notifier=Recorder(), universe_loader=lambda n: universe["now"])
    assert bot2.sleeves[0].symbols == ["A", "C"]


def test_universe_failure_keeps_old_candidates(tmp_path):
    idx = pd.bdate_range("2026-08-01", "2026-10-19")
    c = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    broker = PaperBroker(cash=1_000_000, prices={"A": df}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)

    def boom(n):
        raise RuntimeError("down")

    rec = Recorder()
    bot = TradingBot(broker, [Sleeve(DualMomentumStrategy(lookback=10, top_n=1, rf=0), ["A"], 1.0, "장기",
                                     universe_top=20)], cfg, state_path=tmp_path / "s.json",
                     notifier=rec, universe_loader=boom)
    bot.step(datetime(2026, 10, 1, 9, 10, tzinfo=KST))
    assert "A" in broker.positions
    assert any("자동 갱신 실패" in m for m in rec.messages)


def test_cheap_large_caps(tmp_path):
    from trading_bot.universe import cheap_large_caps

    def rec(code, name, cap, price):
        r = bytearray(record(code, name, cap))
        r[152:159] = f"{price:>7}".encode()
        return bytes(r)

    (tmp_path / "m_new_stock.mst").write_bytes(b"".join([
        rec("005930", "*삼성전자", 5000000, 150000), rec("000660", "*SK하이닉스", 3000000, 600000),
        rec("055550", "*신한지주", 400000, 25000), rec("034020", "*두산에너빌리티", 300000, 18000)]))
    rows = cheap_large_caps(30000, cache_dir=tmp_path)
    assert [(x.code, x.prev_close) for x in rows] == [("055550", 25000), ("034020", 18000)]
