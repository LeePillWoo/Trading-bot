from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, Sleeve, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig
from trading_bot.notify import Notifier


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class Fixed:
    name, warmup = "fixed", 1

    def __init__(self, value):
        self.value = value

    def signals(self, df):
        return pd.Series(self.value, index=df.index)


def prices(last=100.0):
    idx = pd.bdate_range("2026-10-01", "2026-10-19")
    c = pd.Series(100.0, index=idx)
    c.iloc[-1] = last
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


AM = datetime(2026, 10, 19, 9, 10, tzinfo=KST)


def make(tmp_path, core_signal=1, active_signal=1):
    broker = PaperBroker(cash=1_000_000, prices={"A": prices(), "B": prices()}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.5, stop_loss_pct=0.05,
                                                   max_daily_loss_pct=0), commission=0)
    sleeves = [Sleeve(Fixed(core_signal), ["A", "B"], 0.7, "장기"), Sleeve(Fixed(active_signal), ["A"], 0.3, "적극")]
    rec = Recorder()
    bot = TradingBot(broker, sleeves, cfg, state_path=tmp_path / "s.json", notifier=rec)
    return bot, broker, rec


def test_budgets_and_shared_symbol(tmp_path):
    bot, broker, rec = make(tmp_path)
    bot.step(AM)
    led = bot.state.holdings
    # 장기 70만원: A 35만원(50%) → 3500주, B 35만원 → 3500주 / 적극 30만원: A 15만원 → 1500주
    assert led["장기"] == {"A": {"qty": 3500, "price": 100.0}, "B": {"qty": 3500, "price": 100.0}}
    assert led["적극"] == {"A": {"qty": 1500, "price": 100.0}}
    assert broker.positions["A"].qty == 5000
    assert "전략별 현황" in rec.messages[-1] and "[장기]" in rec.messages[-1]


def test_sleeve_sells_only_its_share(tmp_path):
    bot, broker, _ = make(tmp_path)
    bot.step(AM)
    bot.sleeves[1].strategy = Fixed(0)  # 적극만 매도 신호
    bot.state.rebalanced = False
    bot.step(AM)
    assert broker.positions["A"].qty == 3500  # 장기 몫은 그대로
    assert "A" not in bot.state.holdings["적극"]


def test_stop_loss_blocks_per_sleeve(tmp_path):
    bot, broker, _ = make(tmp_path)
    bot.step(AM)
    broker.prices["A"] = prices(90.0)  # -10% → 두 슬리브 모두 손절
    bot.step(AM)
    assert "A" not in broker.positions
    assert {"장기:A", "적극:A"} <= bot.state.blocked


def test_existing_positions_adopted_for_single_strategy(tmp_path):
    import json
    broker = PaperBroker(cash=1_000_000, prices={"A": prices()}, commission=0, sell_tax=0)
    broker.buy("A", 100)  # 업데이트 전부터 들고 있던 종목
    # 장부 기능 이전 버전이 남긴 상태 파일
    (tmp_path / "s.json").write_text(json.dumps({"date": "2026-10-16", "orders": 0, "blocked": []}))
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)
    bot = TradingBot(broker, Fixed(0), cfg, state_path=tmp_path / "s.json", notifier=Recorder())
    bot.step(AM)
    assert "A" not in broker.positions  # 장부로 넘겨받아 매도 신호에 정상 매도


def test_combined_backtest_cli(capsys):
    from trading_bot.__main__ import main
    main(["backtest", "--demo", "--days", "400", "--strategy", "breakout", "--symbols", "A", "B",
          "--core-strategy", "dual_momentum", "--core-param", "lookback=120", "--core-symbols", "A", "B", "C"])
    out = capsys.readouterr().out
    assert "== 합계" in out and "[장기] dual_momentum 70%" in out and "[적극] breakout 30%" in out


def test_switching_to_split_mode_moves_holdings(tmp_path):
    broker = PaperBroker(cash=1_000_000, prices={"A": prices(), "B": prices()}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)
    single = TradingBot(broker, Fixed(1), cfg, state_path=tmp_path / "s.json", notifier=Recorder())
    single.step(AM)
    assert single.state.holdings == {"": {"A": {"qty": 2000, "price": 100.0}}}
    sleeves = [Sleeve(Fixed(1), ["A", "B"], 0.7, "장기"), Sleeve(Fixed(1), ["A"], 0.3, "적극")]
    split = TradingBot(broker, sleeves, cfg, state_path=tmp_path / "s.json", notifier=Recorder())
    split.step(datetime(2026, 10, 20, 9, 10, tzinfo=KST))
    assert split.state.holdings["적극"]["A"]["qty"] == 2000  # 기존 보유분은 적극 장부로
    assert "" not in split.state.holdings


def test_zero_balance_skips_without_marking_month(tmp_path):
    from trading_bot.strategy import DualMomentumStrategy
    idx = pd.bdate_range("2026-08-01", "2026-10-19")
    c = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    broker = PaperBroker(cash=0, prices={"A": df}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)
    rec = Recorder()
    bot = TradingBot(broker, [Sleeve(DualMomentumStrategy(lookback=10, top_n=1, rf=0), ["A"], 0.7, "장기"),
                              Sleeve(Fixed(1), ["A"], 0.3, "적극")], cfg,
                     state_path=tmp_path / "s.json", notifier=rec)
    bot.step(AM)
    assert any("총자산이 0원" in m for m in rec.messages)
    assert bot.state.portfolio_month == {}
    # 자금이 들어온 다음 거래일엔 정상 리밸런싱
    broker.cash = 1_000_000
    bot.state.rebalanced = False
    bot.step(AM)
    assert "A" in bot.state.holdings["장기"]


def test_fresh_account_never_touches_existing_holdings(tmp_path):
    """새로 시작하는 계좌: 원래 들고 있던 종목이 목록에 있고 -80% 여도 손절하지 않는다."""
    broker = PaperBroker(cash=100_000, prices={"A": prices()}, commission=0, sell_tax=0)
    broker.buy("A", 100)
    broker.prices["A"] = prices(20.0)  # -80%
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(stop_loss_pct=0.07, max_daily_loss_pct=0), commission=0)
    for state in (None, {"date": "2026-10-20", "holdings": None}):  # 파일 없음 / 새 버전이 만든 빈 장부
        path = tmp_path / f"s{state is None}.json"
        if state is not None:
            import json
            path.write_text(json.dumps(state))
        bot = TradingBot(broker, Fixed(0), cfg, state_path=path, notifier=Recorder())
        bot.step(AM)
        assert broker.positions["A"].qty == 100


def test_month_not_marked_when_buy_fails_and_rebalance_command(tmp_path):
    from trading_bot.strategy import DualMomentumStrategy
    idx = pd.bdate_range("2026-08-01", "2026-10-19")
    c = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    broker = PaperBroker(cash=10, prices={"A": df, "X": prices()}, commission=0, sell_tax=0)
    broker.cash = 10
    broker.positions = {}
    from trading_bot.broker.base import Position
    broker.positions["X"] = Position("X", 1000, 100.0, 100.0)  # 원래 들고 있던 종목 (총자산 > 0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_daily_loss_pct=0), commission=0)
    rec = Recorder()
    bot = TradingBot(broker, [Sleeve(DualMomentumStrategy(lookback=10, top_n=1, rf=0), ["A"], 1.0, "장기")],
                     cfg, state_path=tmp_path / "s.json", notifier=rec)
    bot.step(AM)
    assert bot.state.portfolio_month == {} and any("리밸런싱 미완료" in m for m in rec.messages)
    broker.cash = 1_000_000  # 현금이 생김
    bot.handle_command("/rebalance")
    bot.step(AM)
    assert "A" in bot.state.holdings["장기"] and bot.state.portfolio_month == {"장기": "2026-10"}
