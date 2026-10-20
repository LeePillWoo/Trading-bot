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
