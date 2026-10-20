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
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .broker.base import Account, Broker
from .config import BotConfig
from .fmt import arrow, b, esc, i, kdate, pct, signed_won, won
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

COMMANDS = [("/status", "간단 상태"), ("/report", "자세한 계좌 보고서"),
            ("/stop", "비상정지 (모든 주문 중단)"), ("/start", "매매 재개"),
            ("/rebalance", "오늘 매매 다시 실행 (장중)"), ("/export", "기기 이사용 상태 내보내기"),
            ("/help", "명령 목록")]


def help_html(extra: list[tuple[str, str]] = ()) -> str:
    return f"📖 {b('명령어')}\n" + "\n".join(f"{c} — {esc(d)}" for c, d in [*COMMANDS, *extra])


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


def keep_awake() -> None:
    """윈도우: 봇이 실행되는 동안 컴퓨터가 절전 모드로 들어가지 않게 한다 (화면은 꺼져도 됨)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("윈도우 절전 방지 설정")
    except Exception:
        log.warning("윈도우 절전 방지 설정 실패", exc_info=True)


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
        # 봇이 사고판 것만의 손익 (입출금·직접 매매 제외). 실현 손익은 매도 시점 현재가 기준 추정치
        self.realized_today: float = data.get("realized_today", 0.0)
        self.realized_total: float = data.get("realized_total", 0.0)
        self.refs_date: str = data.get("refs_date", "")  # 장부의 오늘 기준가(ref)를 맞춘 날짜
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
        # 단타 슬리브 상태 (scalp.ScalpManager 가 관리, 다른 장부와 분리)
        self.scalp: dict = data.get("scalp", {})

    def roll(self, today: str) -> None:
        if self.date != today:
            self.date, self.orders = today, 0
            self.rebalanced = self.close_reported = self.holiday = False
            self.start_equity = 0.0
            self.realized_today = 0.0
            self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "date": self.date, "orders": self.orders, "rebalanced": self.rebalanced,
            "close_reported": self.close_reported, "holiday": self.holiday,
            "start_equity": self.start_equity, "blocked": sorted(self.blocked),
            "realized_today": self.realized_today, "realized_total": self.realized_total,
            "refs_date": self.refs_date,
            "paused": self.paused, "pause_reason": self.pause_reason,
            "portfolio_month": self.portfolio_month, "holdings": self.holdings,
            "universe": self.universe, "scalp": self.scalp,
        }, ensure_ascii=False, indent=2))


def flow_note(df: pd.DataFrame, days: int = 5) -> str:
    """최근 days 일 외국인/기관 순매수 요약."""
    recent = df.tail(days)
    if recent.empty or "foreign" not in recent or recent["foreign"].isna().all():
        return "수급 데이터 없음"
    f, i = recent["foreign"].sum(), recent["institution"].sum()
    return f"외국인 {f:+,.0f}주 · 기관 {i:+,.0f}주"


class TradingBot:
    def __init__(self, broker: Broker, strategy: Strategy | list[Sleeve], config: BotConfig,
                 state_path: str | Path = "bot_state.json", trade_time: dtime = dtime(9, 5),
                 notifier: Notifier | None = None, label: str = "", universe_loader=None,
                 watcher=None, tick_sec: int = 60, scalp_cfg=None):
        self.broker = broker
        # 장중 시세 기록 + 급등 알림 (ticks.TickWatcher). None 이면 끔
        self.watcher = watcher
        self.tick_sec = tick_sec
        self.scalp = None  # 승인형 단타 (scalp.ScalpManager). watcher 가 있어야 동작
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
        if scalp_cfg and scalp_cfg.budget_pct > 0:
            from .scalp import ScalpManager
            self.scalp = ScalpManager(self, scalp_cfg)

    def help(self) -> str:
        from .scalp import SCALP_COMMANDS
        return help_html(SCALP_COMMANDS if self.scalp else ())

    @property
    def strategy(self) -> Strategy:
        return self.sleeves[0].strategy

    @strategy.setter
    def strategy(self, value: Strategy) -> None:
        self.sleeves[0].strategy = value

    def notify(self, text: str, html: bool = False) -> None:
        """텔레그램 전송. html=False 인 일반 문자열은 통째로 이스케이프해서 안전하게 보낸다."""
        body = text if html else esc(text)
        if self.label:
            body = f"{b(f'[{self.label}]')} {body}"
        self.notifier.send(body, html=True)

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
            self.notify(f"⛔ {b('매매 일시정지')}\n{esc(reason)}\n\n/start 로 재개할 수 있습니다.", html=True)

    def resume(self) -> None:
        self.state.paused, self.state.pause_reason = False, ""
        self.state.save()
        self.notify(f"▶️ {b('매매를 재개합니다.')}", html=True)

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

    # ---------- 봇 손익 ----------
    def _price(self, symbol: str, acc: Account) -> float:
        pos = acc.positions.get(symbol)
        return pos.current_price if pos else self.broker.current_price(symbol)

    def bot_pnl(self, acc: Account) -> tuple[float, float, float]:
        """(오늘 봇 손익, 오늘 실현, 보유 평가손익(매수가 대비)). 입출금·직접 매매는 포함되지 않는다."""
        today_unrealized = held_unrealized = 0.0
        for sl in self.sleeves:
            for sym, h in self.ledger(sl).items():
                try:
                    cur = self._price(sym, acc)
                except Exception:
                    continue
                today_unrealized += h["qty"] * (cur - h.get("ref", h["price"]))
                held_unrealized += h["qty"] * (cur - h["price"])
        return self.state.realized_today + today_unrealized, self.state.realized_today, held_unrealized

    # ---------- 보고 서식 (HTML) ----------
    def account_section(self, acc: Account) -> str:
        return f"💰 {b('계좌')}\n총자산 {b(won(acc.equity))} · 현금 {won(acc.cash)}"

    def pnl_section(self, acc: Account) -> str:
        today, realized, held = self.bot_pnl(acc)
        base = self.state.start_equity or acc.equity
        p = f" ({pct(today / base)})" if base else ""
        return (f"📈 {b('봇 손익')} {i('(입출금·직접 매매 제외)')}\n"
                f"오늘 {arrow(today)} {b(f'{today:+,.0f}원')}{p} · 실현 {realized:+,.0f}원\n"
                f"보유 평가 {signed_won(held)} · 누적 실현 {self.state.realized_total:+,.0f}원")

    def _holdings(self, sleeve: Sleeve, acc: Account) -> list[tuple[str, dict, float]]:
        out = []
        for sym, h in self.ledger(sleeve).items():
            try:
                cur = self._price(sym, acc)
            except Exception:
                cur = h["price"]
            out.append((sym, h, cur))
        return out

    def holding_lines(self, sleeve: Sleeve, acc: Account) -> list[str]:
        """종목당 3줄: 손익 요약 / 매수 단가 × 수량 = 매수금액 / 현재 단가 × 수량 = 평가금액."""
        lines = []
        for sym, h, cur in self._holdings(sleeve, acc):
            qty = h["qty"]
            cost, value = h["price"] * qty, cur * qty
            diff = value - cost
            r = diff / cost if cost else 0.0
            lines.append(f"{b(self.label_of(sym))} {qty}주 {arrow(diff)} {b(f'{diff:+,.0f}원')} ({pct(r)})\n"
                         f"  매수 {h['price']:,.0f} × {qty} = {won(cost)}\n"
                         f"  현재 {cur:,.0f} × {qty} = {won(value)}")
        return lines

    def _totals(self, sleeve: Sleeve, acc: Account) -> tuple[float, float]:
        """(매수금액 합계, 평가금액 합계)."""
        rows = self._holdings(sleeve, acc)
        return sum(h["price"] * h["qty"] for _, h, _ in rows), sum(cur * h["qty"] for _, h, cur in rows)

    @staticmethod
    def _total_line(cost: float, value: float) -> str:
        diff = value - cost
        return (f"{i('합계')} 매수 {won(cost)} → 평가 {won(value)} · "
                f"{arrow(diff)} {b(f'{diff:+,.0f}원')} ({pct(diff / cost if cost else 0.0)})")

    def holdings_section(self, acc: Account) -> str:
        """봇이 산 종목 (장부 기준). 산 지 2영업일 전엔 계좌 잔고 목록에 안 보일 수 있어 장부로 보여준다."""
        if len(self.sleeves) == 1:
            sl = self.sleeves[0]
            lines = self.holding_lines(sl, acc)
            if lines:
                lines.append(self._total_line(*self._totals(sl, acc)))
            text = f"📦 {b('봇 보유 종목')}\n" + ("\n".join(lines) if lines else "없음")
        else:
            parts = [f"📦 {b('전략별 현황')}"]
            for sl in self.sleeves:
                equity, _ = self.budget(sl, acc)
                cost, value = self._totals(sl, acc)
                parts.append(f"{b(f'[{sl.name}]')} {esc(sl.strategy.name)} {sl.weight:.0%} · "
                             f"투자 {won(value)} / 한도 {won(equity)}")
                lines = self.holding_lines(sl, acc)
                if lines:
                    lines.append(self._total_line(cost, value))
                parts.append("\n".join(lines) if lines else "보유 없음")
            text = "\n".join(parts)
        managed = {sym for sl in self.sleeves for sym in self.ledger(sl)}
        others = [p for p in acc.positions.values() if p.symbol not in managed]
        if others:
            text += ("\n\nℹ️ " + i("봇 관리 외 보유 (직접 매매분·결제 전 표시):") + " "
                     + ", ".join(f"{esc(self.label_of(p.symbol))} {p.qty}주" for p in others))
        return text

    def account_report(self, acc: Account) -> str:
        return "\n\n".join([self.account_section(acc), self.pnl_section(acc), self.holdings_section(acc)])

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
        if not held:
            return False
        pos = acc.positions.get(symbol)
        # 산 지 2영업일이 안 된 주식은 잔고(결제 기준)에 안 보일 수 있다 → 장부 수량으로 주문하고
        # 실제 매도 가능 수량 제한은 증권사(매도가능수량 조회)에 맡긴다
        qty = min(held["qty"], pos.qty) if pos else held["qty"]
        if not self._can_order():
            return False
        try:
            sell_price = self._price(symbol, acc)
        except Exception:
            sell_price = held["price"]
        res = self.broker.sell(symbol, qty)
        self.state.orders += 1
        if res.ok:
            if res.qty < qty:
                # 증권사가 매도 가능 수량만큼만 받아줌 → 남은 수량은 장부에 두고 계속 관리 (다음 점검 때 재시도)
                held["qty"] -= res.qty
                self.notify(f"ℹ️ {sleeve.tag}{self.label_of(symbol)}: 요청 {qty}주 중 {res.qty}주만 매도 접수. "
                            f"남은 {held['qty']}주는 다음 점검 때 다시 시도합니다.")
            else:
                self.ledger(sleeve).pop(symbol, None)
            self.state.realized_today += res.qty * (sell_price - held.get("ref", held["price"]))
            self.state.realized_total += res.qty * (sell_price - held["price"])
            cost, proceeds = res.qty * held["price"], res.qty * sell_price
            diff = proceeds - cost
            self.notify(f"🔵 {b(f'{sleeve.tag}매도 주문')} {esc(self.label_of(symbol))}\n"
                        f"{res.qty}주 · {esc(reason)}\n"
                        f"매수 {held['price']:,.0f} × {res.qty} = {won(cost)}\n"
                        f"매도 약 {sell_price:,.0f} × {res.qty} = 약 {won(proceeds)}\n"
                        f"{arrow(diff)} {b(f'{diff:+,.0f}원')} ({pct(diff / cost if cost else 0.0)})\n"
                        f"주문번호 {esc(res.order_id)}", html=True)
        elif "수량" in res.message and not pos:
            # 증권사 기준으로도 팔 수량이 없음 (직접 매도한 경우 등) → 장부 정리
            self.ledger(sleeve).pop(symbol, None)
            self.notify(f"ℹ️ {sleeve.tag}{self.label_of(symbol)}: 계좌에 매도 가능 수량이 없어 장부에서 정리했습니다 "
                        f"(직접 매도하셨나요?)")
        else:
            self.notify_error(f"{sleeve.tag}매도 실패 {self.label_of(symbol)}: {res.message}")
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
            self.ledger(sleeve)[symbol] = {"qty": res.qty, "price": price, "ref": price}
            self.notify(f"🔴 {b(f'{sleeve.tag}매수 주문')} {esc(self.label_of(symbol))}\n"
                        f"매수 약 {price:,.0f} × {res.qty} = 약 {won(res.qty * price)}\n"
                        f"주문번호 {esc(res.order_id)}", html=True)
        else:
            self.notify_error(f"{sleeve.tag}매수 실패 {self.label_of(symbol)}: {res.message}")
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
        """손절/익절 (슬리브 장부의 매수가 기준), 하루 손실 한도 (봇 손익 기준, 입출금 제외)."""
        self.init_ledger(acc)
        if not self.state.start_equity:
            self.state.start_equity = acc.equity
            self.state.save()
        if self.state.refs_date != self.state.date:
            # 하루의 시작: 보유 종목의 오늘 손익 기준가를 현재가로 맞춘다
            for sl in self.sleeves:
                for sym, h in self.ledger(sl).items():
                    try:
                        h["ref"] = self._price(sym, acc)
                    except Exception:
                        h["ref"] = h.get("ref", h["price"])
            self.state.refs_date = self.state.date
            self.state.save()
        # 손절이 먼저: 손실 한도로 멈추기 전에 손실 종목부터 정리
        for sl in self.sleeves:
            for sym, held in list(self.ledger(sl).items()):
                pos = acc.positions.get(sym)
                try:  # 결제 전이라 잔고에 안 보이면 현재가를 따로 조회
                    price = pos.current_price if pos else self.broker.current_price(sym)
                except Exception:
                    log.exception("%s 현재가 조회 실패", sym)
                    continue
                reason = exit_reason(held["price"], price, self.cfg.risk)
                if reason and self._sell(sl, sym, acc, reason):
                    self.state.blocked.add(sl.key(sym))
                    self.state.save()
        limit = self.cfg.risk.max_daily_loss_pct
        today, _, _ = self.bot_pnl(acc)
        if limit and self.state.start_equity and today <= -self.state.start_equity * limit:
            self.pause(f"하루 손실 한도 {limit:.0%} 도달 (봇 손익 {today:+,.0f}원, "
                       f"장 시작 자산 {self.state.start_equity:,.0f}원 기준)")

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
        note = "\n".join(f"· {esc(self.label_of(s))} {arrow(mom[s])} {pct(mom[s], 1)}{' ✅' if s in selected else ''}"
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
        head = i(f"후보 갱신: 코스피 시총 상위 {sleeve.universe_top}")
        if not added and not removed:
            return head + " (변동 없음)"
        return (head + "".join([f"\n  편입 {', '.join(esc(self.label_of(s)) for s in added)}" if added else "",
                                f"\n  제외 {', '.join(esc(self.label_of(s)) for s in removed)}" if removed else ""]))

    def label_of(self, symbol: str) -> str:
        return f"{self.names[symbol]}({symbol})" if symbol in self.names else symbol

    def _apply_targets(self, sleeve: Sleeve, targets: dict[str, int], weight: float, reason: str) -> list[str]:
        """목표대로 매도/매수. 사야 했는데 못 산 종목 목록을 반환 (재진입 금지 종목 제외)."""
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
        return [s for s, t in targets.items()
                if t == 1 and s not in self.ledger(sleeve) and sleeve.key(s) not in self.state.blocked]

    def rebalance_sleeve(self, sleeve: Sleeve, now: datetime) -> str | None:
        """슬리브 하나의 오늘 매매. 보고용 문구를 반환, 휴장일이면 None."""
        today = pd.Timestamp(now.date())
        strategy = sleeve.strategy
        if getattr(strategy, "portfolio", False):
            month = now.strftime("%Y-%m")
            if self.state.portfolio_month.get(sleeve.name) == month:
                return f"📅 {b(f'{sleeve.tag}이번 달 리밸런싱 완료')} (다음: 다음 달 첫 거래일)"
            universe_note = self.refresh_universe(sleeve) if sleeve.universe_top else ""
            targets, trading_day, note = self.portfolio_targets(sleeve, today)
            if targets and not trading_day:
                return None
            if not targets:
                return f"⚠️ {esc(sleeve.tag)}데이터 조회 실패 — 다음에 재시도"
            for sym in self.ledger(sleeve):  # 후보에서 빠진 보유 종목은 정리
                targets.setdefault(sym, 0)
            missed = self._apply_targets(sleeve, targets, strategy.weight, "리밸런싱 제외")
            if missed:
                # 현금 부족 등으로 못 산 종목이 있으면 완료로 기록하지 않고 다음 거래일에 다시 시도
                return (f"📅 {b(f'{sleeve.tag}{month} 리밸런싱 미완료')} — 못 산 종목: "
                        f"{', '.join(esc(self.label_of(s)) for s in missed)}\n"
                        + i("(현금 부족 등. 다음 거래일 09:05 또는 /rebalance 로 다시 시도)") + "\n"
                        + (f"{universe_note}\n" if universe_note else "")
                        + f"{i(f'최근 {strategy.lookback}거래일 수익률')}\n{note}")
            self.state.portfolio_month[sleeve.name] = month
            self.state.save()
            held = [esc(self.label_of(s)) for s, t in targets.items() if t] or ["없음 (전부 현금)"]
            return (f"📅 {b(f'{sleeve.tag}{month} 월간 리밸런싱 완료')}\n보유: {', '.join(held)}\n"
                    + (f"{universe_note}\n" if universe_note else "")
                    + f"{i(f'최근 {strategy.lookback}거래일 수익률')}\n{note}")
        self.flow_notes = {}
        targets, trading_day = self.signals_for_today(sleeve, today)
        if targets and not trading_day:
            return None
        if not targets:
            return f"⚠️ {esc(sleeve.tag)}데이터 조회 실패 — 다음에 재시도"
        self._apply_targets(sleeve, targets, self.cfg.risk.max_position_pct, "매도 신호")
        lines = [f"📋 {b(f'{sleeve.tag}신호')}"]
        lines += [f"{'🟢' if t else '⚪'} {esc(self.label_of(s))} — {'보유' if t else '미보유'}"
                  for s, t in targets.items()]
        if self.flow_notes:
            lines.append(i("수급 (최근 5일 순매수)"))
            lines += [f"· {esc(self.label_of(s))} {esc(n)}" for s, n in self.flow_notes.items()]
        return "\n".join(lines)

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
        title = f"☀️ {b('오늘의 매매 완료')} · {kdate(now)}"
        if len(self.sleeves) == 1 and parts[0].startswith("📅"):
            first, _, rest = parts[0].partition("\n")
            title, parts = f"{first} · {kdate(now)}", ([rest] if rest else [])
        self.notify("\n\n".join([title, *parts, self.account_report(acc)]), html=True)

    def status_text(self) -> str:
        """/status: 짧은 상태 (살아 있는지, 일시정지, 오늘 주문, 봇 손익 한 줄)."""
        state = (f"⛔ {b('일시정지')} ({esc(self.state.pause_reason)})" if self.state.paused
                 else f"✅ {b('정상 작동')}")
        try:
            acc = self.broker.account()
            today, _, held = self.bot_pnl(acc)
            n = sum(len(self.ledger(sl)) for sl in self.sleeves)
            money = (f"💰 총자산 {b(won(acc.equity))} · 봇 보유 {n}종목\n"
                     f"📈 오늘 {signed_won(today)} · 보유 평가 {signed_won(held)}")
        except Exception as e:
            money = f"계좌 조회 실패: {esc(e)}"
        scalp = f"\n{self.scalp.status_html()}" if self.scalp else ""
        return (f"{state}\n오늘 주문 {self.state.orders}회 · 오늘 매매 {'완료' if self.state.rebalanced else '대기'}\n"
                f"{money}{scalp}\n\n자세히: /report")

    def report_text(self) -> str:
        """/report: 자세한 보고 (전략 구성, 계좌, 봇 손익, 보유 종목)."""
        try:
            body = self.account_report(self.broker.account())
        except Exception as e:
            body = f"계좌 조회 실패: {esc(e)}"
        return f"📊 {b('계좌 보고서')} · {kdate(datetime.now(KST))}\n{esc(self.describe())}\n\n{body}"

    def describe(self) -> str:
        lines = [f"{sl.tag}전략 {sl.strategy.name}" + (f" 비중 {sl.weight:.0%}" if sl.name else "")
                 + (f" · 후보 코스피 시총 상위 {sl.universe_top} (매달 자동 갱신)" if sl.universe_top
                    else f" · 종목 {', '.join(self.label_of(s) for s in sl.symbols)}")
                 for sl in self.sleeves]
        if self.watcher:
            lines.append(f"👀 {self.watcher.describe()} ({self.tick_sec}초마다)")
        if self.scalp:
            lines.append(self.scalp.describe())
        return "\n".join(lines)

    def tick(self, now: datetime | None = None) -> None:
        """장중 시세 기록 + 급등 감지. 알림만 보내고 주문은 내지 않는다."""
        now = now or datetime.now(KST)
        if not self.watcher or not is_market_open(now) or self.state.holiday:
            return
        for s in self.watcher.poll(now):
            offer = self.scalp.offer(s.symbol, s.price, now) if self.scalp else None
            tail = offer or i("알림만 보냅니다. 봇은 이 종목을 사지 않습니다.")
            self.notify(f"🚨 {b('급등 감지')} {esc(self.label_of(s.symbol))}\n"
                        f"현재가 {won(s.price)} · 전일 대비 {arrow(s.change)} {pct(s.change)}\n"
                        f"거래량 평소의 {s.vol_ratio:.1f}배\n{tail}", html=True)
        if self.scalp:
            self.scalp.on_tick(now)

    def handle_command(self, cmd: str) -> None:
        if cmd == "/stop":
            if self.state.paused:
                self.notify("이미 일시정지 상태입니다.")
            else:
                self.pause("텔레그램 /stop 명령")
        elif cmd == "/start":
            self.resume()
        elif cmd == "/rebalance":
            # 이번 달 리밸런싱 기록을 지우고, 다음 점검(최대 5분 뒤) 때 오늘 매매를 다시 실행
            self.state.rebalanced = False
            self.state.portfolio_month = {}
            self.state.save()
            self.notify(f"🔁 {b('다음 점검 때(최대 5분 안) 오늘 매매를 다시 실행합니다.')} 장중에만 동작합니다.", html=True)
        elif cmd == "/export":
            from .migrate import export_text
            text = export_text([self.state.path])
            self.notify("📦 기기 이사용 상태입니다 (키·토큰 없음). 아래 다음 메시지 전체를 복사해서 "
                        "새 기기에서 `python -m trading_bot import-state` 에 붙여넣으세요.\n"
                        "⚠️ 가져오기 전에 이 기기의 봇은 꺼야 합니다.")
            self.notifier.send(text)  # 라벨 없이 그대로 (복사하기 쉽게)
        elif cmd == "/status":
            self.notify(self.status_text(), html=True)
        elif cmd == "/report":
            self.notify(self.report_text(), html=True)
        elif cmd.startswith("/scalp") and self.scalp:
            self.scalp.command(cmd, datetime.now(KST))
        else:
            self.notify(self.help(), html=True)

    def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(KST)
        self.state.roll(now.date().isoformat())
        if (now.weekday() < 5 and now.time() >= CLOSE_REPORT_TIME and self.state.start_equity
                and not self.state.close_reported and not self.state.holiday):
            self.state.close_reported = True
            self.state.save()
            acc = self.broker.account()
            self.notify(f"🌙 {b('장 마감 보고')} · {kdate(now)}\n\n{self.account_report(acc)}", html=True)
        if not is_market_open(now) or self.state.holiday or self.state.paused:
            return
        self.check_risk(self.broker.account())
        if not self.state.paused and not self.state.rebalanced and now.time() >= self.trade_time:
            self.rebalance(now)
            self.state.rebalanced = True
            self.state.save()

    def load_names(self) -> None:
        """종목명 표시용: 나무증권 종목마스터(공개 파일)에서 전 종목 이름을 받는다. 실패해도 코드만 표시."""
        try:
            from .universe import load_master
            self.names.update({x.code: x.name for x in load_master()})
        except Exception as e:
            log.warning("종목명 불러오기 실패 (코드만 표시): %s", e)

    def run_forever(self) -> None:
        self.load_names()
        log.info("자동매매 시작: %s 점검주기=%ds", self.describe(), self.cfg.interval_sec)
        keep_awake()
        paused = (f"\n\n⛔ {b('현재 일시정지 상태')} ({esc(self.state.pause_reason)}) — /start 로 재개"
                  if self.state.paused else "")
        self.notify(f"🤖 {b('자동매매 봇 시작')}\n{esc(self.describe())}{paused}\n\n{self.help()}", html=True)
        next_step = next_tick = 0.0
        next_heartbeat = time.monotonic() + HEARTBEAT_SEC
        while True:
            if self.watcher and time.monotonic() >= next_tick:
                try:
                    self.tick()
                except Exception as e:
                    log.exception("시세 기록 중 오류")
                    self.notify_error(str(e))
                next_tick = time.monotonic() + self.tick_sec
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
            if getattr(self.notifier, "conflict", False) and not self.state.paused:
                # 같은 계좌로 두 곳에서 동시에 돌면 주문이 두 번 나가고 장부가 엇갈린다
                self.pause("다른 기기(휴대폰/컴퓨터)에서 같은 봇이 동시에 실행 중입니다. "
                           "한 곳만 남기고 끈 뒤 /start")
            # 다음 점검까지 텔레그램 명령을 기다린다 (롱폴링)
            wait = next_step - time.monotonic()
            if self.watcher:
                wait = min(wait, next_tick - time.monotonic())
            wait = max(1.0, min(wait, 30))
            for cmd in self.notifier.poll_commands(timeout=wait):
                try:
                    self.handle_command(cmd)
                except Exception as e:
                    log.exception("명령 처리 오류")
                    self.notify_error(f"{cmd} 처리 실패: {e}")
