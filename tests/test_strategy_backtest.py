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


@pytest.mark.parametrize("name", ["sma", "rsi", "breakout", "sma_flow"])
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


def flow_frame(closes, flows):
    df = frame(closes)
    df["foreign"] = flows
    df["institution"] = 0.0
    return df


def test_sma_flow_needs_positive_flow_to_enter():
    from trading_bot.strategy import SmaFlowStrategy
    closes = [10] * 4 + [11, 12, 13, 14]
    strat = SmaFlowStrategy(short=2, long=4, flow_days=2)
    # 상승 추세지만 수급이 마이너스 → 진입 안 함
    assert strat.signals(flow_frame(closes, [-1.0] * 8)).sum() == 0
    # 수급 플러스 → 진입
    assert strat.signals(flow_frame(closes, [1.0] * 8)).iloc[-1] == 1


def test_sma_flow_exits_on_trend_break_not_flow():
    from trading_bot.strategy import SmaFlowStrategy
    strat = SmaFlowStrategy(short=2, long=4, flow_days=2)
    closes = [10] * 4 + [11, 12, 13, 14, 15]
    flows = [1.0] * 6 + [-5.0] * 3  # 보유 중 수급이 나빠져도 추세가 살아 있으면 유지
    assert strat.signals(flow_frame(closes, flows)).iloc[-1] == 1
    closes2 = closes + [9, 8, 7]
    assert strat.signals(flow_frame(closes2, flows + [1.0] * 3)).iloc[-1] == 0


def test_sma_flow_without_flow_data_errors():
    from trading_bot.strategy import SmaFlowStrategy
    with pytest.raises(ValueError, match="수급"):
        SmaFlowStrategy().signals(frame([10] * 30))
