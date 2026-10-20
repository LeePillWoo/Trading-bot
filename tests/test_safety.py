from datetime import datetime

import pandas as pd

from trading_bot.bot import KST, TradingBot
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import BotConfig, RiskConfig
from trading_bot.notify import Notifier, TelegramNotifier


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class On:
    name, warmup = "on", 1

    def signals(self, df):
        return pd.Series(1, index=df.index)


def prices(end="2026-10-19", last=100.0):
    idx = pd.bdate_range("2026-10-01", end)
    c = pd.Series(100.0, index=idx)
    c.iloc[-1] = last
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def make(tmp_path, end="2026-10-19", **risk):
    broker = PaperBroker(cash=1_000_000, prices={"A": prices(end)}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.5, **risk), commission=0)
    rec = Recorder()
    bot = TradingBot(broker, On(), cfg, state_path=tmp_path / "s.json", notifier=rec)
    return bot, broker, rec


AM = datetime(2026, 10, 19, 9, 10, tzinfo=KST)


def test_stop_command_blocks_all_orders(tmp_path):
    bot, broker, rec = make(tmp_path)
    bot.handle_command("/stop")
    bot.step(AM)
    assert broker.positions == {}
    assert any("일시정지" in m for m in rec.messages)
    bot.handle_command("/start")
    bot.step(AM)
    assert broker.positions["A"].qty == 5000


def test_pause_survives_restart_and_new_day(tmp_path):
    bot, _, _ = make(tmp_path)
    bot.handle_command("/stop")
    bot2, broker2, _ = make(tmp_path)
    bot2.step(datetime(2026, 10, 20, 9, 10, tzinfo=KST))
    assert bot2.state.paused and broker2.positions == {}


def test_daily_loss_limit_pauses(tmp_path):
    bot, broker, rec = make(tmp_path, max_daily_loss_pct=0.03, stop_loss_pct=0)
    bot.step(AM)  # 매수, 시작 자산 1,000,000
    broker.prices["A"] = prices(last=90.0)  # 5000주 * -10 = -50,000 (-5%)
    bot.step(AM)
    assert bot.state.paused and "손실 한도" in bot.state.pause_reason


def test_holiday_no_trading(tmp_path):
    bot, broker, rec = make(tmp_path, end="2026-10-16")  # 10/19 일봉 없음
    bot.step(AM)
    assert broker.positions == {} and bot.state.holiday
    assert any("휴장일" in m for m in rec.messages)


def test_reports(tmp_path):
    bot, _, rec = make(tmp_path)
    bot.step(AM)
    assert any("오늘의 매매 완료" in m and "A 5000주" in m for m in rec.messages)
    bot.step(datetime(2026, 10, 19, 15, 40, tzinfo=KST))
    bot.step(datetime(2026, 10, 19, 15, 50, tzinfo=KST))
    assert sum("장 마감 보고" in m for m in rec.messages) == 1


def test_status_command(tmp_path):
    bot, _, rec = make(tmp_path)
    bot.handle_command("/status")
    assert "정상 작동" in rec.messages[-1] and "총자산" in rec.messages[-1] and "/report" in rec.messages[-1]
    bot.handle_command("/report")
    assert "계좌 보고서" in rec.messages[-1] and "봇 손익" in rec.messages[-1]
    bot.handle_command("/whatever")
    assert "/stop" in rec.messages[-1]


def test_error_notifications_throttled(tmp_path):
    bot, _, rec = make(tmp_path)
    for _ in range(5):
        bot.notify_error("같은 오류")
    assert len(rec.messages) == 1


class FakeTg:
    def __init__(self, updates):
        self.updates, self.sent = updates, []

    def get(self, url, params, timeout):
        class R:
            def json(_):
                return {"ok": True, "result": self.updates}
        return R()

    def post(self, url, json, timeout):
        self.sent.append(json)


def test_telegram_only_accepts_own_chat():
    tg = TelegramNotifier("T", "111", session=FakeTg([
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/status"}},
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/stop"}},
        {"update_id": 3, "message": {"chat": {"id": 111}, "text": "hello"}},
        {"update_id": 4, "message": {"chat": {"id": 111}, "text": "/STOP@mybot"}},
    ]))
    assert tg.poll_commands() == ["/status", "/stop"]
    assert tg._offset == 5
    tg.send("hi")
    assert tg.session.sent[-1] == {"chat_id": "111", "text": "hi"}


def test_service_argv_reads_bot_args(monkeypatch):
    from trading_bot.__main__ import build_parser, service_argv

    monkeypatch.setenv("BOT_ARGS", "--symbols 005930 000660 --stop-loss 0.05")
    argv = service_argv()
    assert argv[:3] == ["run", "--broker", "api"]
    args = build_parser().parse_args(argv)
    assert args.symbols == ["005930", "000660"] and args.stop_loss == 0.05


def test_flow_strategy_report_and_missing_flow(tmp_path):
    from trading_bot.strategy import SmaFlowStrategy

    idx = pd.bdate_range("2026-09-01", "2026-10-19")
    c = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0,
                       "foreign": 100.0, "institution": 50.0})
    broker = PaperBroker(cash=1_000_000, prices={"A": df}, commission=0, sell_tax=0)
    cfg = BotConfig(symbols=["A"], risk=RiskConfig(max_position_pct=0.5), commission=0)
    rec = Recorder()
    bot = TradingBot(broker, SmaFlowStrategy(), cfg, state_path=tmp_path / "s.json", notifier=rec)
    bot.step(AM)
    assert "A" in broker.positions
    report = next(m for m in rec.messages if "오늘의 매매 완료" in m)
    assert "외국인 +500주 · 기관 +250주" in report

    # 수급 데이터가 없으면 신규 매수하지 않고 알림
    broker2 = PaperBroker(cash=1_000_000, prices={"A": df.drop(columns=["foreign", "institution"])},
                          commission=0, sell_tax=0)
    rec2 = Recorder()
    bot2 = TradingBot(broker2, SmaFlowStrategy(), cfg, state_path=tmp_path / "s2.json", notifier=rec2)
    bot2.step(AM)
    assert broker2.positions == {}
    assert any("수급 조회 실패" in m for m in rec2.messages)
