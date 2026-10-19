"""실시간 자동매매 루프.

동작 방식 (백테스트와 동일한 규칙):
- 매 거래일 trade_time(기본 09:05)에 '전일까지 확정된 일봉'으로 신호를 계산하고 시장가로 주문한다.
- 장중에는 interval_sec 마다 보유 종목의 손절/익절과 하루 손실 한도를 점검한다.
- 손절/익절 후에는 신호가 한 번 0이 될 때까지 재진입하지 않는다.
- 설정한 symbols 외의 보유 종목은 건드리지 않는다.

안전장치:
- 일시정지(/stop 또는 하루 손실 한도 도달) 중에는 어떤 주문도 내지 않는다. /start 로 해제.
- 오늘 일봉이 없으면 휴장일로 보고 매매하지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .broker.base import Account, Broker
from .config import BotConfig
from .notify import Notifier
from .risk import exit_reason, position_size
from .strategy import Strategy, with_flow

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)
CLOSE_REPORT_TIME = dtime(15, 35)
ERROR_NOTIFY_INTERVAL = 1800  # 같은 오류는 30분에 한 번만 알림

HELP = ("/status 현재 상태\n/report 계좌 보고서\n/stop 비상정지 (모든 주문 중단)\n"
        "/start 매매 재개\n/help 명령 목록")


def is_market_open(now: datetime) -> bool:
    """평일 정규장 시간 여부 (공휴일은 일봉 데이터로 별도 판단)."""
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


class BotState:
    """재시작해도 유지되어야 하는 상태."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.date: str = data.get("date", "")
        self.orders: int = data.get("orders", 0)
        self.rebalanced: bool = data.get("rebalanced", False)
        self.close_reported: bool = data.get("close_reported", False)
        self.holiday: bool = data.get("holiday", False)
        self.start_equity: float = data.get("start_equity", 0.0)
        self.blocked: set[str] = set(data.get("blocked", []))
        # 일시정지는 날짜가 바뀌어도 유지 (사람이 /start 로 풀어야 함)
        self.paused: bool = data.get("paused", False)
        self.pause_reason: str = data.get("pause_reason", "")

    def roll(self, today: str) -> None:
        if self.date != today:
            self.date, self.orders = today, 0
            self.rebalanced = self.close_reported = self.holiday = False
            self.start_equity = 0.0
            self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "date": self.date, "orders": self.orders, "rebalanced": self.rebalanced,
            "close_reported": self.close_reported, "holiday": self.holiday,
            "start_equity": self.start_equity, "blocked": sorted(self.blocked),
            "paused": self.paused, "pause_reason": self.pause_reason,
        }, ensure_ascii=False, indent=2))


def flow_note(df: pd.DataFrame, days: int = 5) -> str:
    """최근 days 일 외국인/기관 순매수 요약."""
    recent = df.tail(days)
    if recent.empty or "foreign" not in recent or recent["foreign"].isna().all():
        return "수급 데이터 없음"
    f, i = recent["foreign"].sum(), recent["institution"].sum()
    return f"외국인 {f:+,.0f}주 · 기관 {i:+,.0f}주"


def format_account(acc: Account, start_equity: float = 0.0) -> str:
    lines = [f"총자산 {acc.equity:,.0f}원 (현금 {acc.cash:,.0f}원)"]
    if start_equity:
        lines.append(f"오늘 손익 {acc.equity - start_equity:+,.0f}원 ({acc.equity / start_equity - 1:+.2%})")
    if acc.positions:
        for p in acc.positions.values():
            pnl = p.current_price / p.avg_price - 1 if p.avg_price else 0
            lines.append(f"· {p.symbol} {p.qty}주 평단 {p.avg_price:,.0f} 현재 {p.current_price:,.0f} ({pnl:+.2%})")
    else:
        lines.append("· 보유 종목 없음")
    return "\n".join(lines)


class TradingBot:
    def __init__(self, broker: Broker, strategy: Strategy, config: BotConfig,
                 state_path: str | Path = "bot_state.json", trade_time: dtime = dtime(9, 5),
                 notifier: Notifier | None = None, label: str = ""):
        self.broker = broker
        self.strategy = strategy
        self.cfg = config
        self.state = BotState(state_path)
        self.trade_time = trade_time
        self.notifier = notifier or Notifier()
        self.label = label
        self._last_errors: dict[str, float] = {}
        self.flow_notes: dict[str, str] = {}

    def notify(self, text: str) -> None:
        self.notifier.send(f"[{self.label}] {text}" if self.label else text)

    def notify_error(self, text: str) -> None:
        key = text[:120]
        now = time.monotonic()
        if now - self._last_errors.get(key, -ERROR_NOTIFY_INTERVAL) >= ERROR_NOTIFY_INTERVAL:
            self._last_errors[key] = now
            self.notify(f"⚠️ 오류: {text}")

    # ---------- 일시정지 ----------
    def pause(self, reason: str) -> None:
        if not self.state.paused:
            self.state.paused, self.state.pause_reason = True, reason
            self.state.save()
            self.notify(f"⛔ 매매 일시정지: {reason}\n/start 로 재개할 수 있습니다.")

    def resume(self) -> None:
        self.state.paused, self.state.pause_reason = False, ""
        self.state.save()
        self.notify("▶️ 매매를 재개합니다.")

    # ---------- 주문 헬퍼 ----------
    def _can_order(self) -> bool:
        if self.state.paused:
            return False
        if self.state.orders >= self.cfg.risk.max_orders_per_day:
            self.pause(f"하루 최대 주문 횟수({self.cfg.risk.max_orders_per_day}회) 도달")
            return False
        return True

    def _sell_all(self, symbol: str, qty: int, reason: str) -> bool:
        if not self._can_order():
            return False
        res = self.broker.sell(symbol, qty)
        self.state.orders += 1
        self.state.save()
        if res.ok:
            self.notify(f"🔵 매도 주문 {symbol} {res.qty}주 ({reason}) 주문번호 {res.order_id}")
        else:
            self.notify_error(f"매도 실패 {symbol}: {res.message}")
        return res.ok

    def _buy(self, symbol: str, equity: float, cash: float) -> bool:
        if not self._can_order():
            return False
        price = self.broker.current_price(symbol)
        qty = position_size(equity, cash, price, self.cfg.risk, self.cfg.commission)
        if qty <= 0:
            log.info("매수 건너뜀 %s: 주문 가능 수량 0 (현금 %.0f, 가격 %.0f)", symbol, cash, price)
            return False
        res = self.broker.buy(symbol, qty)
        self.state.orders += 1
        self.state.save()
        if res.ok:
            self.notify(f"🔴 매수 주문 {symbol} {res.qty}주 (약 {price:,.0f}원) 주문번호 {res.order_id}")
        else:
            self.notify_error(f"매수 실패 {symbol}: {res.message}")
        return res.ok

    # ---------- 매매 로직 ----------
    def signals_for_today(self, today: pd.Timestamp) -> tuple[dict[str, int], bool]:
        """(종목별 목표 포지션, 오늘 거래일 여부). 오늘 일봉이 하나라도 있으면 거래일."""
        targets, trading_day = {}, False
        self.flow_notes = {}
        for sym in self.cfg.symbols:
            try:
                df = self.broker.daily_ohlcv(sym, days=self.strategy.warmup + 60)
            except Exception as e:
                log.exception("%s 일봉 조회 실패", sym)
                self.notify_error(f"{sym} 일봉 조회 실패: {e}")
                continue
            trading_day |= bool((df.index >= today).any())
            past = df[df.index < today]  # 장중 미완성 봉 제외
            if getattr(self.strategy, "needs_flow", False):
                try:
                    flow = self.broker.investor_flow(sym, days=self.strategy.warmup + 60)
                except Exception as e:
                    log.exception("%s 수급 조회 실패", sym)
                    self.notify_error(f"{sym} 수급 조회 실패 (오늘 신규 매수 안 함): {e}")
                    flow = pd.DataFrame(columns=["foreign", "institution"], dtype=float)
                past = with_flow(past, flow)
                self.flow_notes[sym] = flow_note(past)
            if len(past) < self.strategy.warmup:
                log.warning("%s: 데이터 부족 (%d봉)", sym, len(past))
                targets[sym] = 0
            else:
                targets[sym] = int(self.strategy.signals(past).iloc[-1])
        return targets, trading_day

    def check_risk(self, acc: Account) -> None:
        """손절/익절, 하루 손실 한도."""
        if not self.state.start_equity:
            self.state.start_equity = acc.equity
            self.state.save()
        # 손절이 먼저: 손실 한도로 멈추기 전에 손실 종목부터 정리
        for sym in self.cfg.symbols:
            pos = acc.positions.get(sym)
            if not pos:
                continue
            reason = exit_reason(pos.avg_price, pos.current_price, self.cfg.risk)
            if reason and self._sell_all(sym, pos.qty, reason):
                self.state.blocked.add(sym)
                self.state.save()
        limit = self.cfg.risk.max_daily_loss_pct
        if limit and acc.equity <= self.state.start_equity * (1 - limit):
            self.pause(f"하루 손실 한도 {limit:.0%} 도달 "
                       f"({self.state.start_equity:,.0f} → {acc.equity:,.0f}원)")

    def rebalance(self, now: datetime) -> None:
        targets, trading_day = self.signals_for_today(pd.Timestamp(now.date()))
        if not targets:
            return  # 전 종목 조회 실패 → 다음 점검 때 재시도
        if not trading_day:
            self.state.holiday = True
            self.state.save()
            self.notify("오늘은 휴장일로 판단되어 매매하지 않습니다. (오늘 일봉 없음)")
            return
        for sym, target in targets.items():
            if target == 0 and sym in self.state.blocked:
                log.info("%s: 신호 해제로 재진입 금지 해제", sym)
                self.state.blocked.discard(sym)
        self.state.save()

        acc = self.broker.account()
        for sym, target in targets.items():  # 매도 먼저
            pos = acc.positions.get(sym)
            if target == 0 and pos:
                self._sell_all(sym, pos.qty, "매도 신호")
        acc = self.broker.account()
        cash, equity = acc.cash, acc.equity
        for sym, target in targets.items():
            if target == 1 and sym not in acc.positions and sym not in self.state.blocked:
                if self._buy(sym, equity, cash):
                    acc = self.broker.account()
                    cash = acc.cash

        signal_text = ", ".join(f"{s}:{'보유' if t else '미보유'}" for s, t in targets.items())
        flow_text = "".join(f"\n· {s} {n}" for s, n in self.flow_notes.items())
        if flow_text:
            flow_text = "\n\n수급 (최근 5일 순매수)" + flow_text
        self.notify(f"☀️ 오늘의 매매 완료\n신호: {signal_text}{flow_text}\n\n주문 후 잔고\n"
                    f"{format_account(self.broker.account(), self.state.start_equity)}")

    def status_text(self) -> str:
        state = f"⛔ 일시정지 ({self.state.pause_reason})" if self.state.paused else "✅ 정상 작동"
        try:
            acc_text = format_account(self.broker.account(), self.state.start_equity)
        except Exception as e:
            acc_text = f"계좌 조회 실패: {e}"
        return (f"{state}\n전략 {self.strategy.name} · 종목 {', '.join(self.cfg.symbols)}\n"
                f"오늘 주문 {self.state.orders}회 · 리밸런싱 {'완료' if self.state.rebalanced else '대기'}\n\n"
                f"{acc_text}")

    def handle_command(self, cmd: str) -> None:
        if cmd == "/stop":
            if self.state.paused:
                self.notify("이미 일시정지 상태입니다.")
            else:
                self.pause("텔레그램 /stop 명령")
        elif cmd == "/start":
            self.resume()
        elif cmd in ("/status", "/report"):
            self.notify(self.status_text())
        else:
            self.notify(HELP)

    def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(KST)
        self.state.roll(now.date().isoformat())
        if (now.weekday() < 5 and now.time() >= CLOSE_REPORT_TIME and self.state.start_equity
                and not self.state.close_reported and not self.state.holiday):
            self.state.close_reported = True
            self.state.save()
            self.notify(f"🌙 장 마감 보고\n{format_account(self.broker.account(), self.state.start_equity)}")
        if not is_market_open(now) or self.state.holiday or self.state.paused:
            return
        self.check_risk(self.broker.account())
        if not self.state.paused and not self.state.rebalanced and now.time() >= self.trade_time:
            self.rebalance(now)
            self.state.rebalanced = True
            self.state.save()

    def run_forever(self) -> None:
        log.info("자동매매 시작: 종목=%s 전략=%s 점검주기=%ds",
                 self.cfg.symbols, self.strategy.name, self.cfg.interval_sec)
        paused = f"\n⛔ 현재 일시정지 상태 ({self.state.pause_reason}) — /start 로 재개" if self.state.paused else ""
        self.notify(f"🤖 자동매매 봇 시작\n전략 {self.strategy.name} · 종목 {', '.join(self.cfg.symbols)}"
                    f"{paused}\n\n{HELP}")
        next_step = 0.0
        while True:
            if time.monotonic() >= next_step:
                try:
                    self.step()
                except Exception as e:
                    log.exception("루프 실행 중 오류")
                    self.notify_error(str(e))
                next_step = time.monotonic() + self.cfg.interval_sec
            # 다음 점검까지 텔레그램 명령을 기다린다 (롱폴링)
            wait = max(1.0, min(next_step - time.monotonic(), 30))
            for cmd in self.notifier.poll_commands(timeout=wait):
                try:
                    self.handle_command(cmd)
                except Exception as e:
                    log.exception("명령 처리 오류")
                    self.notify_error(f"{cmd} 처리 실패: {e}")
