from datetime import datetime, time as dtime

from trading_bot.bot import KST, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig
from trading_bot.scalp import ScalpConfig
from trading_bot.ticks import TickWatcher
from tests.test_bot import FixedSignal
from tests.test_ticks import Sink, bars

T = lambda h, m: datetime(2026, 10, 19, h, m, tzinfo=KST)  # noqa: E731


def make(tmp_path, **cfg):
    broker = PaperBroker(cash=1_000_000, prices={"A": bars(105.0, 1000.0)}, commission=0, sell_tax=0)
    w = TickWatcher(broker, ["A"], data_dir=tmp_path / "ticks")
    sink = Sink()
    bot = TradingBot(broker, FixedSignal(0), BotConfig(symbols=["A"], commission=0, sell_tax=0),
                     state_path=tmp_path / "s.json", notifier=sink, watcher=w,
                     scalp_cfg=ScalpConfig(budget_pct=0.1, **cfg))
    bot.scalp.log_path = tmp_path / "scalp.csv"
    return bot, broker, sink


def set_price(broker, price):
    broker.prices["A"] = bars(price, 1000.0)


def test_spike_offers_and_yes_buys(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    assert "/scalp_yes" in sink.sent[-1] and broker.positions == {}  # 승인 전엔 안 삼
    bot.scalp.command("/scalp_yes", T(10, 1))
    assert broker.positions["A"].qty == 952  # 100만 × 10% / 105
    assert "단타 매수" in sink.sent[-1]
    assert bot.state.scalp["positions"]["A"]["qty"] == 952
    assert bot.ledger(bot.sleeves[0]) == {}  # breakout 장부와 분리


def test_offer_expires(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 11))  # 10분 지남
    assert broker.positions == {} and "없습니다" in sink.sent[-1]


def test_take_profit_closes_and_logs(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    set_price(broker, 108.5)  # +3.3%
    bot.scalp.on_tick(T(10, 5))
    assert broker.positions == {} and bot.state.scalp["positions"] == {}
    assert "목표 수익" in sink.sent[-1]
    assert bot.state.scalp["pnl_today"] > 3000 and bot.state.realized_today > 3000
    assert (tmp_path / "scalp.csv").read_text().count("\n") == 2


def test_trailing_stop(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    set_price(broker, 107.0)
    bot.scalp.on_tick(T(10, 5))
    assert "A" in broker.positions
    set_price(broker, 105.3)  # 고점 107 대비 -1.6%
    bot.scalp.on_tick(T(10, 6))
    assert broker.positions == {} and "고점 대비" in sink.sent[-1]


def test_force_close_before_market_close_and_no_entry_after(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    bot.scalp.on_tick(T(15, 20))
    assert broker.positions == {} and "장 마감 전 정리" in sink.sent[-1]
    bot.scalp.s["pending"]["A"] = {"expires": T(15, 40).isoformat(), "price": 105.0}
    bot.scalp.command("/scalp_yes", T(15, 21))
    assert broker.positions == {}


def test_daily_loss_limit_stops_scalp(tmp_path):
    bot, broker, sink = make(tmp_path, max_daily_loss_pct=0.001, stop_loss=0.01)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    set_price(broker, 103.0)
    bot.scalp.on_tick(T(10, 5))
    assert bot.state.scalp["stopped"]
    assert bot.scalp.offer("A", 103.0, T(10, 6)) is None
    assert bot.state.paused is False  # 단타만 멈추고 기존 전략은 계속


def test_scalp_off_closes_everything(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    bot.handle_command("/scalp_off")
    assert broker.positions == {} and bot.state.scalp["enabled"] is False
    bot.tick(T(10, 30))
    assert "/scalp_yes" not in sink.sent[-1]  # 꺼진 동안은 제안하지 않음


def test_paused_bot_does_not_enter(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.pause("test")
    bot.scalp.command("/scalp_yes", T(10, 1))
    assert broker.positions == {}


def test_state_survives_restart(tmp_path):
    bot, broker, sink = make(tmp_path)
    bot.tick(T(10, 0))
    bot.scalp.command("/scalp_yes", T(10, 1))
    bot2 = TradingBot(broker, FixedSignal(0), BotConfig(symbols=["A"]), state_path=tmp_path / "s.json",
                      notifier=Sink(), watcher=bot.watcher, scalp_cfg=ScalpConfig(budget_pct=0.1))
    assert bot2.state.scalp["positions"]["A"]["qty"] == 952
    assert "단타" in bot2.status_text() and "/scalp_yes" in bot2.help()


def test_default_scalp_is_off(tmp_path):
    broker = PaperBroker(cash=1_000_000, prices={"A": bars(105.0, 1000.0)})
    bot = TradingBot(broker, FixedSignal(0), BotConfig(symbols=["A"]), state_path=tmp_path / "s.json",
                     notifier=Sink(), watcher=TickWatcher(broker, ["A"], data_dir=tmp_path / "t"))
    assert bot.scalp is None and "/scalp" not in bot.help()
