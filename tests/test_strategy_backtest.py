import pandas as pd
import pytest

from trading_bot.backtest import run_backtest
from trading_bot.config import RiskConfig
from trading_bot.data import synthetic_ohlcv
from trading_bot.strategy import RsiStrategy, SmaCrossStrategy, make_strategy


def frame(closes):
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def test_sma_cross_signal():
    df = frame([10] * 5 + [11, 12, 13, 14, 15])
    sig = SmaCrossStrategy(short=2, long=4).signals(df)
    assert sig.iloc[:4].eq(0).all()  # warmup
    assert sig.iloc[-1] == 1


@pytest.mark.parametrize("name", ["sma", "rsi", "breakout"])
def test_no_lookahead(name):
    """미래 데이터를 추가해도 과거 신호는 바뀌지 않아야 한다."""
    df = synthetic_ohlcv(300, seed=3)
    strat = make_strategy(name)
    full = strat.signals(df)
    partial = strat.signals(df.iloc[:200])
    pd.testing.assert_series_equal(full.iloc[:200], partial)


def test_rsi_bounds():
    rsi = RsiStrategy.rsi(synthetic_ohlcv(200)["close"], 14).dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()


def test_backtest_executes_next_open_with_costs():
    df = frame([10, 10, 10, 20, 20])
    df["open"] = [10, 10, 10, 30, 20]

    class AlwaysOn:
        name, warmup = "on", 1
        def signals(self, d):
            return pd.Series(1, index=d.index)

    risk = RiskConfig(max_position_pct=1.0, stop_loss_pct=0)
    res = run_backtest({"X": df}, AlwaysOn(), cash=1000, risk=risk, commission=0, sell_tax=0)
    t = res.trades[0]
    assert t.entry_date == df.index[1] and t.entry_price == 10 and t.qty == 100
    assert res.equity.iloc[-1] == pytest.approx(2000)


def test_backtest_stop_loss_blocks_reentry():
    df = frame([100, 100, 100, 80, 80, 80, 80])

    class AlwaysOn:
        name, warmup = "on", 1
        def signals(self, d):
            return pd.Series(1, index=d.index)

    risk = RiskConfig(max_position_pct=1.0, stop_loss_pct=0.1)
    res = run_backtest({"X": df}, AlwaysOn(), cash=1000, risk=risk, commission=0, sell_tax=0)
    assert len(res.trades) == 1
    assert res.trades[0].reason.startswith("손절")


def test_stats_keys():
    data = {"A": synthetic_ohlcv(300, seed=1), "B": synthetic_ohlcv(300, seed=2)}
    stats = run_backtest(data, SmaCrossStrategy()).stats()
    assert stats["거래횟수"] >= 1
    assert -1 <= stats["최대낙폭(MDD)"] <= 0
