"""실시간 자동매매 루프.

동작 방식 (백테스트와 동일한 규칙):
- 매 거래일 trade_time(기본 09:05)에 '전일까지 확정된 일봉'으로 신호를 계산하고 시장가로 주문한다.
- 장중에는 interval_sec 마다 보유 종목의 손절/익절을 점검한다.
- 손절/익절 후에는 신호가 한 번 0이 될 때까지 재진입하지 않는다.
- 설정한 symbols 외의 보유 종목은 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .broker.base import Broker
from .config import BotConfig
from .risk import exit_reason, position_size
from .strategy import Strategy

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)


def is_market_open(now: datetime) -> bool:
    """평일 정규장 시간 여부 (공휴일은 브로커 주문 실패로 처리됨)."""
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


class BotState:
    """재시작해도 유지되어야 하는 상태 (당일 매매 여부, 주문 횟수, 재진입 금지 종목)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.date: str = data.get("date", "")
        self.orders: int = data.get("orders", 0)
        self.rebalanced: bool = data.get("rebalanced", False)
        self.blocked: set[str] = set(data.get("blocked", []))

    def roll(self, today: str) -> None:
        if self.date != today:
            self.date, self.orders, self.rebalanced = today, 0, False
            self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "date": self.date, "orders": self.orders,
            "rebalanced": self.rebalanced, "blocked": sorted(self.blocked),
        }, ensure_ascii=False, indent=2))


class TradingBot:
    def __init__(self, broker: Broker, strategy: Strategy, config: BotConfig,
                 state_path: str | Path = "bot_state.json", trade_time: dtime = dtime(9, 5)):
        self.broker = broker
        self.strategy = strategy
        self.cfg = config
        self.state = BotState(state_path)
        self.trade_time = trade_time

    # ---------- 주문 헬퍼 ----------
    def _can_order(self) -> bool:
        if self.state.orders >= self.cfg.risk.max_orders_per_day:
            log.warning("일일 최대 주문 횟수(%d)에 도달해 주문을 중단합니다", self.cfg.risk.max_orders_per_day)
            return False
        return True

    def _sell_all(self, symbol: str, qty: int, reason: str) -> bool:
        if not self._can_order():
            return False
        res = self.broker.sell(symbol, qty)
        self.state.orders += 1
        self.state.save()
        if res.ok:
            log.info("매도 %s %d주 (%s) 주문번호=%s", symbol, qty, reason, res.order_id)
        else:
            log.error("매도 실패 %s: %s", symbol, res.message)
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
            log.info("매수 %s %d주 @~%.0f 주문번호=%s", symbol, qty, price, res.order_id)
        else:
            log.error("매수 실패 %s: %s", symbol, res.message)
        return res.ok

    # ---------- 매매 로직 ----------
    def target_position(self, symbol: str, today: pd.Timestamp) -> int:
        df = self.broker.daily_ohlcv(symbol, days=self.strategy.warmup + 60)
        df = df[df.index < today]  # 장중 미완성 봉 제외
        if len(df) < self.strategy.warmup:
            log.warning("%s: 데이터 부족 (%d봉)", symbol, len(df))
            return 0
        return int(self.strategy.signals(df).iloc[-1])

    def check_exits(self) -> None:
        acc = self.broker.account()
        for sym in self.cfg.symbols:
            pos = acc.positions.get(sym)
            if not pos:
                continue
            reason = exit_reason(pos.avg_price, pos.current_price, self.cfg.risk)
            if reason and self._sell_all(sym, pos.qty, reason):
                self.state.blocked.add(sym)
                self.state.save()

    def rebalance(self, now: datetime) -> None:
        today = pd.Timestamp(now.date())
        targets = {}
        for sym in self.cfg.symbols:
            try:
                targets[sym] = self.target_position(sym, today)
            except Exception:
                log.exception("%s 신호 계산 실패", sym)
        for sym, target in targets.items():
            if target == 0 and sym in self.state.blocked:
                log.info("%s: 신호 해제로 재진입 금지 해제", sym)
                self.state.blocked.discard(sym)
        self.state.save()
        log.info("목표 포지션: %s", targets)

        acc = self.broker.account()
        # 매도 먼저
        for sym, target in targets.items():
            pos = acc.positions.get(sym)
            if target == 0 and pos:
                self._sell_all(sym, pos.qty, "신호")
        # 매수
        acc = self.broker.account()
        cash, equity = acc.cash, acc.equity
        for sym, target in targets.items():
            if target == 1 and sym not in acc.positions and sym not in self.state.blocked:
                if self._buy(sym, equity, cash):
                    # 다음 종목 수량 계산을 위해 사용한 현금을 반영
                    acc = self.broker.account()
                    cash = acc.cash

    def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(KST)
        self.state.roll(now.date().isoformat())
        if not is_market_open(now):
            return
        self.check_exits()
        if not self.state.rebalanced and now.time() >= self.trade_time:
            self.rebalance(now)
            self.state.rebalanced = True
            self.state.save()

    def run_forever(self) -> None:
        log.info("자동매매 시작: 종목=%s 전략=%s 점검주기=%ds",
                 self.cfg.symbols, self.strategy.name, self.cfg.interval_sec)
        while True:
            try:
                self.step()
            except Exception:
                log.exception("루프 실행 중 오류")
            time.sleep(self.cfg.interval_sec)
