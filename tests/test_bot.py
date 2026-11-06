from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, TradingBot, is_market_open
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig


class FixedSignal:
    name, warmup = "fixed", 1

    def __init__(self, value):
        self.value = value

    def signals(self, df):
        return pd.Series(self.value, index=df.index)


def prices(last_close=100.0):
    idx = pd.bdate_range("2026-10-01", "2026-10-19")
    c = pd.Series(100.0, index=idx)
    c.iloc[-1] = last_close
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def make_bot(tmp_path, signal=1, last_close=100.0, **risk):
    broker = PaperBroker(cash=1_000_000, prices={"A": prices(last_close), "B": prices(last_close)},
                         commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A", "B"], risk=RiskConfig(max_position_pct=0.5, **risk), commission=0)
    return TradingBot(broker, FixedSignal(signal), cfg, state_path=tmp_path / "s.json"), broker


MONDAY_0910 = datetime(2026, 10, 19, 9, 10, tzinfo=KST)


def test_market_hours():
    assert is_market_open(MONDAY_0910)
    assert not is_market_open(datetime(2026, 10, 18, 10, 0, tzinfo=KST))  # 일요일
    assert not is_market_open(datetime(2026, 10, 19, 15, 31, tzinfo=KST))


def test_buys_once_per_day(tmp_path):
    bot, broker = make_bot(tmp_path)
    bot.step(MONDAY_0910)
    bot.step(MONDAY_0910)
    assert {s: p.qty for s, p in broker.positions.items()} == {"A": 5000, "B": 5000}
    assert bot.state.orders == 2


def test_before_trade_time_does_nothing(tmp_path):
    bot, broker = make_bot(tmp_path)
    bot.step(datetime(2026, 10, 19, 9, 1, tzinfo=KST))
    assert broker.positions == {}


def test_sells_on_flat_signal(tmp_path):
    bot, broker = make_bot(tmp_path)
    bot.step(MONDAY_0910)
    bot.strategy = FixedSignal(0)
    bot.state.rebalanced = False
    bot.step(MONDAY_0910)
    assert broker.positions == {}


def test_stop_loss_and_no_reentry(tmp_path):
    bot, broker = make_bot(tmp_path, stop_loss_pct=0.05)
    bot.step(MONDAY_0910)
    broker.prices["A"] = prices(90.0)  # -10%
    bot.step(MONDAY_0910)
    assert "A" not in broker.positions and "A" in bot.state.blocked
    bot.state.rebalanced = False
    bot.step(MONDAY_0910)
    assert "A" not in broker.positions


def test_max_orders_per_day(tmp_path):
    bot, broker = make_bot(tmp_path, max_orders_per_day=1)
    bot.step(MONDAY_0910)
    assert len(broker.positions) == 1


def test_ignores_other_holdings(tmp_path):
    bot, broker = make_bot(tmp_path, signal=0)
    broker.prices["Z"] = prices()
    broker.buy("Z", 10)
    bot.step(MONDAY_0910)
    assert broker.positions["Z"].qty == 10


def test_report_shows_change_vs_previous_close(tmp_path, monkeypatch):
    from trading_bot.fmt import strip_tags
    bot, broker = make_bot(tmp_path)
    bot.step(MONDAY_0910)  # 100원에 매수
    # 일봉 마지막 행을 오늘로, 그 앞(전일 종가)을 95로 두고 현재가 102
    df = prices(102.0)
    df.index = df.index[:-1].append(pd.DatetimeIndex([pd.Timestamp(datetime.now(KST).date())]))
    df.iloc[-2, df.columns.get_loc("close")] = 95.0
    broker.prices["A"] = df
    bot._prev_closes.clear()  # 09:05 보고 때 캐시된 값 비움
    text = strip_tags(bot.holding_lines(bot.sleeves[0], broker.account())[0])
    assert "전일 대비 🔺 +35,000원 (+7.37%)" in text  # (102-95) × 5000주
    assert "+10,000원 (+2.00%)" in text  # 매수가 대비는 그대로


def test_max_single_buys_one_expensive_share(tmp_path):
    # 총자산 100만, 종목 몫 10% = 10만 → 1주 15만원짜리는 원래 못 삼
    broker = PaperBroker(cash=1_000_000, prices={"A": prices(150_000.0)}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.1), commission=0)
    bot = TradingBot(broker, FixedSignal(1), cfg, state_path=tmp_path / "s.json")
    bot.step(MONDAY_0910)
    assert broker.positions == {}
    cfg2 = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.1, max_single_pct=0.2), commission=0)
    bot2 = TradingBot(broker, FixedSignal(1), cfg2, state_path=tmp_path / "s2.json")
    bot2.step(MONDAY_0910)
    assert broker.positions["A"].qty == 1  # 15만 ≤ 100만 × 20%
    cfg3 = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.1, max_single_pct=0.1), commission=0)
    broker3 = PaperBroker(cash=1_000_000, prices={"A": prices(150_000.0)}, commission=0, sell_tax=0)
    TradingBot(broker3, FixedSignal(1), cfg3, state_path=tmp_path / "s3.json").step(MONDAY_0910)
    assert broker3.positions == {}  # 15만 > 100만 × 10% → 안 삼


def test_report_lists_todays_trades_with_realized_pnl(tmp_path):
    from trading_bot.fmt import strip_tags
    bot, broker = make_bot(tmp_path)
    bot.step(MONDAY_0910)  # A, B 각 5000주 100원에 매수
    broker.prices["A"] = prices(110.0)
    bot.strategy = FixedSignal(0)
    bot.state.rebalanced = False
    bot.step(MONDAY_0910)  # 전부 매도: A +10%, B 0%
    text = strip_tags(bot.account_report(broker.account()))
    assert "오늘 매매" in text
    assert "매수 A 100 × 5000 = 500,000원" in text
    assert "매도 A 5000주 🔺 +50,000원 (+10.00%)" in text and "100 → 약 110" in text
    assert "매도 합계 🔺 +50,000원 (+5.00%)" in text
    bot.state.roll("2026-10-20")
    assert bot.state.trades_today == [] and "오늘 매매" not in strip_tags(bot.account_report(broker.account()))


def test_equity_counts_unsettled_bot_positions(tmp_path):
    """오늘 산 종목이 결제 전이라 잔고에 안 보여도(증권사 순자산 = 현금) 총자산·매수 예산이 맞아야 한다."""
    from trading_bot.broker.base import Account

    class Unsettled(PaperBroker):
        def account(self):
            acc = super().account()
            return Account(cash=acc.cash, positions={}, total=acc.cash)  # 보유 목록 비어 있고 순자산 = 현금

    broker = Unsettled(cash=1_000_000, prices={"A": prices(), "B": prices(), "C": prices()},
                       commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A", "B", "C"], risk=RiskConfig(max_position_pct=0.3), commission=0)
    bot = TradingBot(broker, FixedSignal(1), cfg, state_path=tmp_path / "s.json")
    bot.step(MONDAY_0910)
    # 예전에는 첫 매수 뒤 총자산을 현금(70만)으로 읽어 예산이 0 → 나머지 못 삼
    assert {s: p.qty for s, p in broker.positions.items()} == {"A": 3000, "B": 3000, "C": 3000}
    assert bot.bot_equity(broker.account()) == 1_000_000


def test_circuit_breaker_rejection_retries_later(tmp_path):
    from trading_bot.broker.base import OrderResult

    class Halted(PaperBroker):
        halted = True

        def buy(self, symbol, qty, price=0):
            if self.halted:
                return OrderResult(symbol, "buy", qty, price, ok=False,
                                   message="[21824] CB 1단계 발동중으로 주문처리가 불가합니다.")
            return super().buy(symbol, qty, price)

    broker = Halted(cash=1_000_000, prices={"A": prices(), "B": prices()}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A", "B"], risk=RiskConfig(max_position_pct=0.5), commission=0)
    sent = []
    bot = TradingBot(broker, FixedSignal(1), cfg, state_path=tmp_path / "s.json")
    bot.notifier.send = lambda text, html=False: sent.append(text)
    bot.step(MONDAY_0910)
    assert broker.positions == {} and not bot.state.rebalanced and bot.state.orders == 0
    assert sum("거래 중단" in m for m in sent) == 1  # 종목마다 오류 알림을 보내지 않고 한 번만
    assert not any("오류" in m for m in sent)
    broker.halted = False  # 20분 뒤 거래 재개
    bot.step(MONDAY_0910)
    assert set(broker.positions) == {"A", "B"} and bot.state.rebalanced
    assert any("오늘의 매매 완료" in m for m in sent)


def test_circuit_breaker_gives_up_after_retries(tmp_path):
    from trading_bot.bot import HALT_RETRIES
    from trading_bot.broker.base import OrderResult

    class Halted(PaperBroker):
        def buy(self, symbol, qty, price=0):
            return OrderResult(symbol, "buy", qty, price, ok=False, message="CB 2단계 발동")

    broker = Halted(cash=1_000_000, prices={"A": prices()}, commission=0, sell_tax=0)
    bot = TradingBot(broker, FixedSignal(1), BotConfig(symbols=["A"]), state_path=tmp_path / "s.json")
    for _ in range(HALT_RETRIES):
        bot.step(MONDAY_0910)
    assert bot.state.rebalanced and bot.state.halt_tries == HALT_RETRIES
