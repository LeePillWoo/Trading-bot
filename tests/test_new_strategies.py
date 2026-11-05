from datetime import datetime

import pandas as pd
import pytest

from trading_bot.backtest import run_backtest
from trading_bot.bot import KST, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig
from trading_bot.data import synthetic_ohlcv
from trading_bot.fmt import strip_tags
from trading_bot.notify import Notifier
from trading_bot.strategy import DualMomentumStrategy, VolatilityBreakoutStrategy


def ohlc(rows, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def test_vb_entry_price_and_gap():
    df = ohlc([(100, 110, 90, 105),   # 어제 범위 20
               (100, 115, 98, 112),   # 목표 100+10=110, 고가 115 → 110 매수
               (120, 125, 119, 121),  # 목표 120+8.5, 고가 125 → 128.5 미도달
               (130, 140, 129, 135)])  # 목표 130+3=133 → 133 매수
    e = VolatilityBreakoutStrategy(k=0.5).entry_prices(df)
    assert pd.isna(e.iloc[0]) and e.iloc[1] == 110 and pd.isna(e.iloc[2]) and e.iloc[3] == 133


def test_vb_no_lookahead():
    df = synthetic_ohlcv(200, seed=5)
    s = VolatilityBreakoutStrategy(k=0.5, ma=5)
    pd.testing.assert_series_equal(s.entry_prices(df).iloc[:150], s.entry_prices(df.iloc[:150]))


def test_vb_backtest_sells_next_open_with_costs():
    df = ohlc([(100, 110, 90, 105), (100, 115, 98, 112), (120, 121, 119, 120)])
    risk = RiskConfig(max_position_pct=1.0)
    res = run_backtest({"X": df}, VolatilityBreakoutStrategy(k=0.5), cash=1100, risk=risk,
                       commission=0, sell_tax=0.002)
    t = res.trades[0]
    assert (t.entry_price, t.qty, t.exit_price) == (110, 10, 120)
    assert res.equity.iloc[-1] == pytest.approx(1100 - 1100 + 1200 * 0.998)


def test_dual_momentum_select():
    import numpy as np
    line = lambda end: pd.Series(np.linspace(100, end, 253))
    closes = {"A": line(130), "B": line(102), "C": line(80), "D": line(110)}
    # 상위 2개(A +30%, D +10%), 연 3% 허들 통과
    assert DualMomentumStrategy(lookback=252, top_n=2, rf=0.03).select(closes) == ["A", "D"]
    # 상위 3개 중 B(+2%)는 허들 3% 미달이라 제외, C(-20%)는 순위 밖
    assert DualMomentumStrategy(lookback=252, top_n=3, rf=0.03).select(closes) == ["A", "D"]
    # 전부 허들 미달이면 현금
    assert DualMomentumStrategy(lookback=252, top_n=2, rf=0.5).select(closes) == []


def monthly_data():
    idx = pd.bdate_range("2024-01-01", "2024-04-30")
    up = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    down = pd.Series(range(300, 300 - len(idx), -1), index=idx, dtype=float)
    mk = lambda c: pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    return {"UP": mk(up), "DOWN": mk(down)}


def test_dual_momentum_backtest_rebalances_monthly():
    res = run_backtest(monthly_data(), DualMomentumStrategy(lookback=10, top_n=1, rf=0.0),
                       cash=1_000_000, commission=0, sell_tax=0)
    assert [t.symbol for t in res.trades] == ["UP"]  # 상승 종목만, 한 번 사서 계속 보유
    assert res.trades[0].entry_date == pd.Timestamp("2024-02-01")  # 달 바뀐 첫 거래일


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text, **kw):
        self.messages.append(strip_tags(text))


def test_bot_dual_momentum_once_per_month(tmp_path):
    data = monthly_data()
    broker = PaperBroker(cash=1_000_000, prices=data, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["UP", "DOWN"], risk=RiskConfig(stop_loss_pct=0, max_daily_loss_pct=0),
                    commission=0)
    rec = Recorder()
    bot = TradingBot(broker, DualMomentumStrategy(lookback=10, top_n=1, rf=0.0), cfg,
                     state_path=tmp_path / "s.json", notifier=rec)
    bot.step(datetime(2024, 4, 1, 9, 10, tzinfo=KST))
    assert set(broker.positions) == {"UP"}
    assert broker.positions["UP"].qty > 0
    assert any("월간 리밸런싱 완료" in m for m in rec.messages)
    # 같은 달 다음 날은 리밸런싱 안 함
    n = len(rec.messages)
    bot.state.rebalanced = False
    bot.step(datetime(2024, 4, 2, 9, 10, tzinfo=KST))
    assert not any("월간 리밸런싱" in m for m in rec.messages[n:])


def test_cli_rejects_vb_live():
    from trading_bot.__main__ import main
    with pytest.raises(ValueError, match="백테스트 전용"):
        main(["run", "--strategy", "vb"])


def test_sell_tax_option_reaches_backtest(capsys):
    from trading_bot.__main__ import main
    main(["backtest", "--demo", "--days", "300", "--strategy", "vb", "--symbols", "A"])
    taxed = capsys.readouterr().out
    main(["backtest", "--demo", "--days", "300", "--strategy", "vb", "--symbols", "A", "--sell-tax", "0"])
    untaxed = capsys.readouterr().out
    final = lambda out: int(next(l for l in out.splitlines() if "최종자산" in l).split(":")[1].replace(",", ""))
    assert final(untaxed) > final(taxed)


def test_slippage_reduces_returns():
    data = {"A": synthetic_ohlcv(300, seed=2)}
    strat = VolatilityBreakoutStrategy(k=0.5)
    base = run_backtest(data, strat).equity.iloc[-1]
    slipped = run_backtest(data, strat, slippage=0.001).equity.iloc[-1]
    assert slipped < base


def test_rotation_rebalances_weekly_in_backtest():
    import numpy as np
    from trading_bot.backtest import run_backtest
    from trading_bot.config import RiskConfig
    from trading_bot.strategy import make_strategy

    idx = pd.bdate_range("2025-01-01", periods=200)
    rng = np.random.default_rng(1)
    data = {}
    for k, s in enumerate("ABCD"):
        c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.001 * k, 0.02, len(idx)))), index=idx)
        data[s] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    rot = make_strategy("rotation", lookback=20, top_n=1, rf=0.0)
    monthly = make_strategy("dual_momentum", lookback=20, top_n=1, rf=0.0)
    r1 = run_backtest(data, rot, cash=1_000_000, risk=RiskConfig(stop_loss_pct=0))
    r2 = run_backtest(data, monthly, cash=1_000_000, risk=RiskConfig(stop_loss_pct=0))
    assert len(r1.trades) > len(r2.trades) > 0
    assert rot.period_key(pd.Timestamp("2026-10-22")) == "2026-W43"
    assert monthly.period_key(pd.Timestamp("2026-10-22")) == "2026-10"


def test_market_filter_goes_to_cash_when_most_candidates_fall():
    from trading_bot.strategy import make_strategy
    idx = pd.bdate_range("2026-01-01", periods=80)
    up = pd.Series([100.0 + k for k in range(80)], index=idx)
    down = pd.Series([200.0 - k for k in range(80)], index=idx)
    rot = make_strategy("rotation", lookback=20, top_n=1, rf=0.0, market_ma=40)
    assert rot.warmup == 41
    assert rot.select({"A": up, "B": down, "C": down}) == []  # 3개 중 1개만 이평 위 → 현금
    assert rot.select({"A": up, "B": up, "C": down}) == ["A"]
    assert make_strategy("rotation", lookback=20, top_n=1, rf=0.0).select({"A": up, "B": down, "C": down}) == ["A"]


def test_portfolio_backtest_stop_loss():
    from trading_bot.backtest import run_backtest
    from trading_bot.config import RiskConfig
    from trading_bot.strategy import make_strategy

    idx = pd.bdate_range("2026-01-05", periods=60)
    a = pd.Series([100.0 + k for k in range(60)], index=idx)
    b = pd.Series(50.0, index=idx)
    crash = idx.get_loc(pd.Timestamp("2026-03-11"))  # 수요일
    a.iloc[crash:] = a.iloc[crash - 1] * 0.5  # -50% 급락 후 유지 (매수가 대비 약 -37%)
    data = {s: pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
            for s, c in {"A": a, "B": b}.items()}
    strat = make_strategy("rotation", lookback=10, top_n=1, rf=0.0)
    no_stop = run_backtest(data, strat, cash=1_000_000, risk=RiskConfig(stop_loss_pct=0), commission=0, sell_tax=0)
    stop = run_backtest(data, strat, cash=1_000_000, risk=RiskConfig(stop_loss_pct=0.15), commission=0, sell_tax=0)
    stopped = [t for t in stop.trades if t.reason == "손절"]
    assert len(stopped) == 1 and stopped[0].exit_date == pd.Timestamp("2026-03-11")
    assert stopped[0].exit_price < stopped[0].entry_price * 0.85 + 1e-9  # 갭하락이라 시가(손절가 아래)에 매도
    assert all(t.reason != "손절" for t in no_stop.trades)


def test_explain_reasons():
    from trading_bot.strategy import make_strategy
    idx = pd.bdate_range("2026-01-01", periods=60)
    # 하락 → 짧은 반등: 중간에 RSI 30 아래, 마지막은 아직 70 아래
    c = pd.Series([100 - k * 2 for k in range(50)] + [2 + k for k in range(1, 11)], index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    rsi = make_strategy("rsi")
    sig = int(rsi.signals(df).iloc[-1])
    text = rsi.explain(df, sig)
    assert sig == 1 and "과매도(30 아래)" in text and "어제 RSI" in text
    up = pd.Series([100 + k * 2 for k in range(60)], index=idx, dtype=float)
    df_up = pd.DataFrame({"open": up, "high": up, "low": up, "close": up, "volume": 1.0})
    assert "과매수" in rsi.explain(df_up, 0)
    assert "20일 최고가 돌파" in make_strategy("breakout").explain(df_up, 1)
    assert "상승 추세" in make_strategy("sma").explain(df_up, 1)

    rot = make_strategy("rotation", lookback=20, top_n=1, rf=0.0)
    closes = {"A": up, "B": c}
    r = rot.reasons(closes)
    assert "1위 (상위 1)" in r["A"] and "(상위 1 밖)" in r["B"]


def test_sell_and_buy_reasons_reach_report(tmp_path):
    from trading_bot.bot import KST, TradingBot
    from trading_bot.broker.paper import PaperBroker
    from trading_bot.config import BotConfig, RiskConfig
    from trading_bot.strategy import make_strategy
    idx = pd.bdate_range("2026-07-01", "2026-10-19")  # 마지막 행 = 오늘 (거래일 판정용)
    down = pd.Series([200 - k for k in range(len(idx))], index=idx, dtype=float)
    down.iloc[-4:] = down.iloc[-5] + pd.Series(range(1, 5), index=down.index[-4:]) * 0.5
    df = pd.DataFrame({"open": down, "high": down, "low": down, "close": down, "volume": 1.0})
    broker = PaperBroker(cash=1_000_000, prices={"A": df}, commission=0, sell_tax=0)
    rec = []
    bot = TradingBot(broker, make_strategy("rsi"), BotConfig(symbols=["A"], risk=RiskConfig(stop_loss_pct=0)),
                     state_path=tmp_path / "s.json")
    bot.notifier.send = lambda text, html=False: rec.append(strip_tags(text))
    bot.step(datetime(2026, 10, 19, 9, 10, tzinfo=KST))
    assert "A" in broker.positions
    assert any("매수 주문" in m and "이유:" in m and "과매도" in m for m in rec)
    assert bot.state.trades_today[0]["reason"]


def _oversold(steep: bool, flow: float = 0.0, n: int = 60):
    """같은 날 하루 급락으로 둘 다 과매도(RSI<30)가 되지만, steep=True 쪽이 더 크게 빠져 RSI 가 더 낮다."""
    idx = pd.bdate_range("2026-07-20", periods=n)
    zigzag = [100.0 + (k % 2) for k in range(n - 1)]
    c = pd.Series(zigzag + [zigzag[-1] - (40.0 if steep else 12.0)], index=idx)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0,
                         "foreign": flow, "institution": 0.0})


def test_rsi_priority_orders_buys_live(tmp_path):
    from trading_bot.bot import KST, TradingBot
    from trading_bot.broker.paper import PaperBroker
    from trading_bot.config import BotConfig, RiskConfig
    from trading_bot.strategy import make_strategy
    # B 가 더 많이 빠짐(RSI 더 낮음)
    data = {"A": _oversold(False), "B": _oversold(True)}
    last = data["A"].index[-1]
    for k in data:  # 오늘 봉 추가 (거래일 판정)
        row = data[k].iloc[[-1]].copy()
        row.index = [last + pd.offsets.BDay(1)]
        data[k] = pd.concat([data[k], row])
    day = datetime(*(last + pd.offsets.BDay(1)).timetuple()[:3], 9, 10, tzinfo=KST)

    def run(strategy, path):
        broker = PaperBroker(cash=1_000_000, prices={k: v.copy() for k, v in data.items()}, commission=0, sell_tax=0)
        bot = TradingBot(broker, strategy, BotConfig(symbols=["A", "B"], risk=RiskConfig(max_position_pct=0.6,
                         stop_loss_pct=0)), state_path=tmp_path / path)
        bot.notifier.send = lambda *a, **k: None
        bot.step(day)
        return list(broker.positions)[0]  # 먼저 산 종목

    assert run(make_strategy("rsi", rank=0), "a.json") == "A"  # 목록 순서
    assert run(make_strategy("rsi"), "b.json") == "B"  # RSI 낮은 B 먼저
    # flow=1: A 만 외국인 순매수 → A 먼저
    data["A"]["foreign"] = 1000.0
    assert run(make_strategy("rsi", flow=1), "c.json") == "A"


def test_rsi_priority_orders_buys_backtest():
    from trading_bot.backtest import run_backtest
    from trading_bot.config import RiskConfig
    from trading_bot.strategy import make_strategy
    def nxt(df):  # 신호 다음 날 시가에 체결되므로 하루를 더 붙인다
        row = df.iloc[[-1]].copy()
        row.index = [df.index[-1] + pd.offsets.BDay(1)]
        return pd.concat([df, row])

    data = {"A": nxt(_oversold(False)), "B": nxt(_oversold(True))}
    risk = RiskConfig(max_position_pct=0.6, stop_loss_pct=0)
    first = lambda st: run_backtest(data, st, cash=1_000_000, risk=risk).trades[0].symbol  # noqa: E731
    assert first(make_strategy("rsi", rank=0)) == "A"
    assert first(make_strategy("rsi")) == "B"
    data["A"]["foreign"] = 1000.0
    assert first(make_strategy("rsi", flow=1)) == "A"
