"""실시간 자동매매 루프.

동작 방식 (백테스트와 동일한 규칙):
- 매 거래일 trade_time(기본 09:05)에 '전일까지 확정된 일봉'으로 신호를 계산하고 시장가로 주문한다.
- 장중에는 interval_sec 마다 보유 종목의 손절/익절과 하루 손실 한도를 점검한다.
- 손절/익절 후에는 신호가 한 번 0이 될 때까지 재진입하지 않는다.
- 설정한 symbols 외의 보유 종목은 건드리지 않는다.

슬리브(sleeve): 계좌를 비중별로 나눠 전략을 여러 개 돌릴 수 있다 (예: 장기 70% / 적극 30%).
- 슬리브마다 자기가 산 수량을 장부(state.holdings)에 따로 기록하고, 팔 때도 자기 몫만 판다.
- 슬리브 자금 = 총자산 × 비중, 그중 이미 보유한 평가액을 뺀 만큼만 새로 산다.

안전장치:
- 일시정지(/stop 또는 하루 손실 한도 도달) 중에는 어떤 주문도 내지 않는다. /start 로 해제.
- 오늘 일봉이 없으면 휴장일로 보고 매매하지 않는다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
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
HEARTBEAT_SEC = 3600  # 1시간마다 로그에 작동 기록

HELP = ("/status 현재 상태\n/report 계좌 보고서\n/stop 비상정지 (모든 주문 중단)\n"
        "/start 매매 재개\n/help 명령 목록")


def is_market_open(now: datetime) -> bool:
    """평일 정규장 시간 여부 (공휴일은 일봉 데이터로 별도 판단)."""
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


@dataclass
class Sleeve:
    strategy: Strategy
    symbols: list[str]
    weight: float = 1.0
    name: str = ""  # 여러 개일 때 "장기"/"적극" 등. 하나면 빈 문자열
    universe_top: int = 0  # >0 이면 리밸런싱마다 후보를 코스피 시총 상위 N 으로 자동 갱신

    def key(self, symbol: str) -> str:
        """재진입 금지(blocked) 기록용 키."""
        return f"{self.name}:{symbol}" if self.name else symbol

    @property
    def tag(self) -> str:
        return f"[{self.name}] " if self.name else ""


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
        # 듀얼 모멘텀: 슬리브별 마지막 리밸런싱 달 (YYYY-MM)
        months = data.get("portfolio_month", {})
        self.portfolio_month: dict[str, str] = months if isinstance(months, dict) else {"": months}
        # 슬리브별 보유 장부: {슬리브: {종목: {"qty": 수량, "price": 매수가}}}
        # None = 장부가 없던 이전 버전의 상태 파일 → init_ledger 에서 기존 보유분을 넘겨받음.
        # 새로 시작하는 계좌는 빈 장부로 시작해서, 원래 들고 있던 종목은 절대 건드리지 않는다.
        if "holdings" in data:
            self.holdings: dict[str, dict[str, dict]] | None = data["holdings"] or {}
        else:
            self.holdings = None if data else {}
        # 슬리브별 마지막으로 자동 선정한 후보 종목
        self.universe: dict[str, list[str]] = data.get("universe", {})
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
            "portfolio_month": self.portfolio_month, "holdings": self.holdings,
            "universe": self.universe,
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
    def __init__(self, broker: Broker, strategy: Strategy | list[Sleeve], config: BotConfig,
                 state_path: str | Path = "bot_state.json", trade_time: dtime = dtime(9, 5),
                 notifier: Notifier | None = None, label: str = "", universe_loader=None):
        self.broker = broker
        self.sleeves = strategy if isinstance(strategy, list) else [Sleeve(strategy, list(config.symbols))]
        if universe_loader is None:
            from .universe import kospi_top_symbols as universe_loader
        self.universe_loader = universe_loader
        self.names: dict[str, str] = {}
        self.cfg = config
        self.state = BotState(state_path)
        for sl in self.sleeves:  # 재시작 시 마지막 자동 선정 후보 복원
            if sl.universe_top and self.state.universe.get(sl.name):
                sl.symbols = list(self.state.universe[sl.name])
        self.trade_time = trade_time
        self.notifier = notifier or Notifier()
        self.label = label
        self._last_errors: dict[str, float] = {}
        self.flow_notes: dict[str, str] = {}

    @property
    def strategy(self) -> Strategy:
        return self.sleeves[0].strategy

    @strategy.setter
    def strategy(self, value: Strategy) -> None:
        self.sleeves[0].strategy = value

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

    # ---------- 장부 ----------
    def ledger(self, sleeve: Sleeve) -> dict[str, dict]:
        if self.state.holdings is None:
            self.state.holdings = {}
        return self.state.holdings.setdefault(sleeve.name, {})

    def init_ledger(self, acc: Account) -> None:
        """장부 준비.
        - 장부가 없던 이전 버전에서 넘어올 때: 전략이 하나면 그 종목의 기존 보유분을 장부로 가져온다.
        - 운용 방식을 바꿔 장부 이름이 달라졌으면(전략 하나 ↔ 장기/적극) 그 종목을 가진 슬리브로 옮긴다
          (겹치면 적극 쪽 우선).
        """
        if self.state.holdings is not None:
            names = {sl.name for sl in self.sleeves}
            for old in [k for k in self.state.holdings if k not in names]:
                for sym, h in self.state.holdings.pop(old).items():
                    target = next((sl for sl in reversed(self.sleeves) if sym in sl.symbols), None)
                    if target is None:
                        log.warning("%s: 운용 방식 변경 후 담당 전략이 없어 장부에서 제외 (계좌에는 그대로)", sym)
                        continue
                    cur = self.ledger(target).get(sym)
                    if cur:
                        cur["qty"] += h["qty"]
                    else:
                        self.ledger(target)[sym] = dict(h)
                self.state.save()
            return
        self.state.holdings = {}
        if len(self.sleeves) == 1:
            sl = self.sleeves[0]
            for sym in sl.symbols:
                if pos := acc.positions.get(sym):
                    self.ledger(sl)[sym] = {"qty": pos.qty, "price": pos.avg_price}
        self.state.save()

    def budget(self, sleeve: Sleeve, acc: Account) -> tuple[float, float]:
        """(슬리브 자금, 슬리브가 새로 쓸 수 있는 현금)."""
        equity = acc.equity * sleeve.weight
        held = sum(h["qty"] * (acc.positions[s].current_price if s in acc.positions else h["price"])
                   for s, h in self.ledger(sleeve).items())
        return equity, max(0.0, min(equity - held, acc.cash))

    # ---------- 주문 헬퍼 ----------
    def _can_order(self) -> bool:
        if self.state.paused:
            return False
        if self.state.orders >= self.cfg.risk.max_orders_per_day:
            self.pause(f"하루 최대 주문 횟수({self.cfg.risk.max_orders_per_day}회) 도달")
            return False
        return True

    def _sell(self, sleeve: Sleeve, symbol: str, acc: Account, reason: str) -> bool:
        """슬리브 장부에 있는 자기 몫만 매도."""
        held = self.ledger(sleeve).get(symbol)
        pos = acc.positions.get(symbol)
        qty = min(held["qty"], pos.qty) if held and pos else 0
        if qty <= 0:
            self.ledger(sleeve).pop(symbol, None)  # 계좌에 없으면 장부 정리
            self.state.save()
            return False
        if not self._can_order():
            return False
        res = self.broker.sell(symbol, qty)
        self.state.orders += 1
        if res.ok:
            self.ledger(sleeve).pop(symbol, None)
            self.notify(f"🔵 {sleeve.tag}매도 주문 {symbol} {res.qty}주 ({reason}) 주문번호 {res.order_id}")
        else:
            self.notify_error(f"{sleeve.tag}매도 실패 {symbol}: {res.message}")
        self.state.save()
        return res.ok

    def _buy(self, sleeve: Sleeve, symbol: str, equity: float, cash: float, weight: float) -> bool:
        if not self._can_order():
            return False
        price = self.broker.current_price(symbol)
        qty = position_size(equity, cash, price, replace(self.cfg.risk, max_position_pct=weight),
                            self.cfg.commission)
        if qty <= 0:
            log.info("매수 건너뜀 %s: 주문 가능 수량 0 (현금 %.0f, 가격 %.0f)", symbol, cash, price)
            return False
        res = self.broker.buy(symbol, qty)
        self.state.orders += 1
        if res.ok:
            self.ledger(sleeve)[symbol] = {"qty": res.qty, "price": price}
            self.notify(f"🔴 {sleeve.tag}매수 주문 {symbol} {res.qty}주 (약 {price:,.0f}원) 주문번호 {res.order_id}")
        else:
            self.notify_error(f"{sleeve.tag}매수 실패 {symbol}: {res.message}")
        self.state.save()
        return res.ok

    # ---------- 매매 로직 ----------
    def signals_for_today(self, sleeve: Sleeve, today: pd.Timestamp) -> tuple[dict[str, int], bool]:
        """(종목별 목표 포지션, 오늘 거래일 여부). 오늘 일봉이 하나라도 있으면 거래일."""
        strategy = sleeve.strategy
        targets, trading_day = {}, False
        for sym in sleeve.symbols:
            try:
                df = self.broker.daily_ohlcv(sym, days=strategy.warmup + 60)
            except Exception as e:
                log.exception("%s 일봉 조회 실패", sym)
                self.notify_error(f"{sym} 일봉 조회 실패: {e}")
                continue
            trading_day |= bool((df.index >= today).any())
            past = df[df.index < today]  # 장중 미완성 봉 제외
            if getattr(strategy, "needs_flow", False):
                try:
                    flow = self.broker.investor_flow(sym, days=strategy.warmup + 60)
                except Exception as e:
                    log.exception("%s 수급 조회 실패", sym)
                    self.notify_error(f"{sym} 수급 조회 실패 (오늘 신규 매수 안 함): {e}")
                    flow = pd.DataFrame(columns=["foreign", "institution"], dtype=float)
                past = with_flow(past, flow)
                self.flow_notes[sym] = flow_note(past)
            if len(past) < strategy.warmup:
                log.warning("%s: 데이터 부족 (%d봉)", sym, len(past))
                targets[sym] = 0
            else:
                targets[sym] = int(strategy.signals(past).iloc[-1])
        return targets, trading_day

    def check_risk(self, acc: Account) -> None:
        """손절/익절 (슬리브 장부의 매수가 기준), 하루 손실 한도 (계좌 전체)."""
        self.init_ledger(acc)
        if not self.state.start_equity:
            self.state.start_equity = acc.equity
            self.state.save()
        # 손절이 먼저: 손실 한도로 멈추기 전에 손실 종목부터 정리
        for sl in self.sleeves:
            for sym, held in list(self.ledger(sl).items()):
                pos = acc.positions.get(sym)
                if not pos:
                    continue
                reason = exit_reason(held["price"], pos.current_price, self.cfg.risk)
                if reason and self._sell(sl, sym, acc, reason):
                    self.state.blocked.add(sl.key(sym))
                    self.state.save()
        limit = self.cfg.risk.max_daily_loss_pct
        if limit and acc.equity <= self.state.start_equity * (1 - limit):
            self.pause(f"하루 손실 한도 {limit:.0%} 도달 "
                       f"({self.state.start_equity:,.0f} → {acc.equity:,.0f}원)")

    def portfolio_targets(self, sleeve: Sleeve, today: pd.Timestamp) -> tuple[dict[str, int], bool, str]:
        """듀얼 모멘텀: (목표 포지션, 거래일 여부, 보고용 모멘텀 요약)."""
        strategy = sleeve.strategy
        closes, trading_day = {}, False
        for sym in sleeve.symbols:
            try:
                df = self.broker.daily_ohlcv(sym, days=strategy.warmup + 30)
            except Exception as e:
                log.exception("%s 일봉 조회 실패", sym)
                self.notify_error(f"{sym} 일봉 조회 실패: {e}")
                continue
            trading_day |= bool((df.index >= today).any())
            closes[sym] = df.loc[df.index < today, "close"]
        if len(closes) < len(sleeve.symbols):
            return {}, trading_day, ""  # 일부 종목 조회 실패 → 다음 점검 때 재시도
        selected = strategy.select(closes)
        mom = strategy.momentum(closes)
        note = "\n".join(f"· {self.label_of(s)} {mom[s]:+.1%}{' ✅' if s in selected else ''}"
                         for s in sorted(mom, key=mom.get, reverse=True))
        return {s: int(s in selected) for s in sleeve.symbols}, trading_day, note

    def refresh_universe(self, sleeve: Sleeve) -> str:
        """후보 종목을 코스피 시총 상위 N 으로 갱신. 보고용 문구 반환."""
        try:
            symbols, names = self.universe_loader(sleeve.universe_top)
        except Exception as e:
            log.exception("후보 종목 갱신 실패")
            self.notify_error(f"{sleeve.tag}후보 종목 자동 갱신 실패, 기존 후보 사용: {e}")
            return ""
        if not symbols:
            return ""
        self.names.update(names)
        added = [s for s in symbols if s not in sleeve.symbols]
        removed = [s for s in sleeve.symbols if s not in symbols]
        sleeve.symbols = list(symbols)
        self.state.universe[sleeve.name] = list(symbols)
        self.state.save()
        head = f"후보 갱신: 코스피 시총 상위 {sleeve.universe_top}"
        if not added and not removed:
            return head + " (변동 없음)"
        return (head + "".join([f"\n  편입 {', '.join(self.label_of(s) for s in added)}" if added else "",
                                f"\n  제외 {', '.join(self.label_of(s) for s in removed)}" if removed else ""]))

    def label_of(self, symbol: str) -> str:
        return f"{self.names[symbol]}({symbol})" if symbol in self.names else symbol

    def _apply_targets(self, sleeve: Sleeve, targets: dict[str, int], weight: float, reason: str) -> None:
        for sym, target in targets.items():
            if target == 0 and sleeve.key(sym) in self.state.blocked:
                log.info("%s: 신호 해제로 재진입 금지 해제", sym)
                self.state.blocked.discard(sleeve.key(sym))
        self.state.save()
        acc = self.broker.account()
        for sym, target in targets.items():  # 매도 먼저
            if target == 0 and sym in self.ledger(sleeve):
                self._sell(sleeve, sym, acc, reason)
        acc = self.broker.account()
        for sym, target in targets.items():
            if target == 1 and sym not in self.ledger(sleeve) and sleeve.key(sym) not in self.state.blocked:
                equity, cash = self.budget(sleeve, acc)
                if self._buy(sleeve, sym, equity, cash, weight):
                    acc = self.broker.account()

    def rebalance_sleeve(self, sleeve: Sleeve, now: datetime) -> str | None:
        """슬리브 하나의 오늘 매매. 보고용 문구를 반환, 휴장일이면 None."""
        today = pd.Timestamp(now.date())
        strategy = sleeve.strategy
        if getattr(strategy, "portfolio", False):
            month = now.strftime("%Y-%m")
            if self.state.portfolio_month.get(sleeve.name) == month:
                return f"{sleeve.tag}이번 달 리밸런싱 완료 (다음: 다음 달 첫 거래일)"
            universe_note = self.refresh_universe(sleeve) if sleeve.universe_top else ""
            targets, trading_day, note = self.portfolio_targets(sleeve, today)
            if targets and not trading_day:
                return None
            if not targets:
                return f"{sleeve.tag}데이터 조회 실패 — 다음에 재시도"
            for sym in self.ledger(sleeve):  # 후보에서 빠진 보유 종목은 정리
                targets.setdefault(sym, 0)
            self._apply_targets(sleeve, targets, strategy.weight, "리밸런싱 제외")
            self.state.portfolio_month[sleeve.name] = month
            self.state.save()
            held = [s for s, t in targets.items() if t] or ["없음 (전부 현금)"]
            held = [self.label_of(s) if s in self.names else s for s in held]
            return (f"📅 {sleeve.tag}{month} 월간 리밸런싱 완료\n보유: {', '.join(held)}\n"
                    + (f"{universe_note}\n" if universe_note else "")
                    + f"최근 {strategy.lookback}거래일 수익률\n{note}")
        self.flow_notes = {}
        targets, trading_day = self.signals_for_today(sleeve, today)
        if targets and not trading_day:
            return None
        if not targets:
            return f"{sleeve.tag}데이터 조회 실패 — 다음에 재시도"
        self._apply_targets(sleeve, targets, self.cfg.risk.max_position_pct, "매도 신호")
        text = f"{sleeve.tag}신호: " + ", ".join(f"{s}:{'보유' if t else '미보유'}" for s, t in targets.items())
        if self.flow_notes:
            text += "\n수급 (최근 5일 순매수)" + "".join(f"\n· {s} {n}" for s, n in self.flow_notes.items())
        return text

    def rebalance(self, now: datetime) -> None:
        acc = self.broker.account()
        if acc.equity <= 0:
            # 아무것도 못 사는데 '이번 달 리밸런싱 완료'로 기록되면 자금이 들어와도 다음 달까지 대기하게 됨
            self.notify("⚠️ 계좌 총자산이 0원이라 오늘 매매를 건너뜁니다. "
                        "자금이 들어오면 다음 거래일 09:05에 자동으로 진행합니다.")
            return
        parts = []
        for sleeve in self.sleeves:
            part = self.rebalance_sleeve(sleeve, now)
            if part is None:
                self.state.holiday = True
                self.state.save()
                self.notify("오늘은 휴장일로 판단되어 매매하지 않습니다. (오늘 일봉 없음)")
                return
            parts.append(part)
        acc = self.broker.account()
        title = "☀️ 오늘의 매매 완료"
        if len(self.sleeves) == 1 and parts[0].startswith("📅"):
            title, parts = parts[0].split("\n", 1)[0], [parts[0].split("\n", 1)[1]]
        self.notify(f"{title}\n" + "\n\n".join(parts) + "\n\n주문 후 잔고\n"
                    f"{format_account(acc, self.state.start_equity)}{self.sleeve_summary(acc)}")

    def sleeve_summary(self, acc: Account) -> str:
        if len(self.sleeves) == 1:
            return ""
        lines = ["\n\n전략별 현황"]
        for sl in self.sleeves:
            equity, _ = self.budget(sl, acc)
            items = []
            value = 0.0
            for sym, h in self.ledger(sl).items():
                cur = acc.positions[sym].current_price if sym in acc.positions else h["price"]
                value += h["qty"] * cur
                items.append(f"{sym} {h['qty']}주 ({cur / h['price'] - 1:+.1%})")
            lines.append(f"[{sl.name}] {sl.strategy.name} 비중 {sl.weight:.0%} · 투자 {value:,.0f}원 / "
                         f"한도 {equity:,.0f}원")
            lines.append("· " + (", ".join(items) if items else "보유 없음"))
        return "\n".join(lines)

    def status_text(self) -> str:
        state = f"⛔ 일시정지 ({self.state.pause_reason})" if self.state.paused else "✅ 정상 작동"
        try:
            acc = self.broker.account()
            acc_text = format_account(acc, self.state.start_equity) + self.sleeve_summary(acc)
        except Exception as e:
            acc_text = f"계좌 조회 실패: {e}"
        return (f"{state}\n{self.describe()}\n"
                f"오늘 주문 {self.state.orders}회 · 리밸런싱 {'완료' if self.state.rebalanced else '대기'}\n\n"
                f"{acc_text}")

    def describe(self) -> str:
        return "\n".join(f"{sl.tag}전략 {sl.strategy.name}" + (f" 비중 {sl.weight:.0%}" if sl.name else "")
                         + (f" · 후보 코스피 시총 상위 {sl.universe_top} (매달 자동 갱신)" if sl.universe_top
                            else f" · 종목 {', '.join(sl.symbols)}") for sl in self.sleeves)

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
            acc = self.broker.account()
            self.notify(f"🌙 장 마감 보고\n{format_account(acc, self.state.start_equity)}{self.sleeve_summary(acc)}")
        if not is_market_open(now) or self.state.holiday or self.state.paused:
            return
        self.check_risk(self.broker.account())
        if not self.state.paused and not self.state.rebalanced and now.time() >= self.trade_time:
            self.rebalance(now)
            self.state.rebalanced = True
            self.state.save()

    def run_forever(self) -> None:
        log.info("자동매매 시작: %s 점검주기=%ds", self.describe(), self.cfg.interval_sec)
        paused = f"\n⛔ 현재 일시정지 상태 ({self.state.pause_reason}) — /start 로 재개" if self.state.paused else ""
        self.notify(f"🤖 자동매매 봇 시작\n{self.describe()}{paused}\n\n{HELP}")
        next_step = 0.0
        next_heartbeat = time.monotonic() + HEARTBEAT_SEC
        while True:
            if time.monotonic() >= next_heartbeat:
                # 꺼진 시각을 나중에 로그로 알 수 있도록 주기적으로 남긴다
                log.info("작동 중 (일시정지=%s)", self.state.paused)
                next_heartbeat = time.monotonic() + HEARTBEAT_SEC
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
