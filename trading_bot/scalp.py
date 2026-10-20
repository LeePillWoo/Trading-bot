"""단타(승인형) 슬리브 — 뼈대.

흐름:  🚨 급등 감지 → "단타 켤까요?" → /scalp_yes → 매수 → 틱마다 점검 → 종료 조건 → 매도 → 보고
- 봇이 혼자 들어가지 않는다. 알림마다 사람이 /scalp_yes 를 보낸 종목만, 그것도 제한 시간 안에만 산다.
- 종료 조건(모두 옵션): 목표 수익, 손절, 고점 대비 하락(추적 손절), 장 마감 전 강제 정리(close_time),
  /scalp_off (즉시 전량 정리). 단타 종목은 절대 다음 날로 넘기지 않는다.
- 안전장치: 1회 예산 = 총자산 × budget_pct, 하루 최대 횟수, 하루 단타 손실 한도(넘으면 그날 단타 종료),
  봇 일시정지(/stop) 중에는 새로 사지 않는다.
- 장부는 breakout 등 다른 슬리브와 완전히 분리(state.scalp). 매매 내역은 data/scalp_trades.csv 에 남겨
  나중에 규칙을 검증할 때 쓴다.

"언제 사고 언제 파는지" 숫자(ScalpConfig)는 분 단위 데이터가 쌓인 뒤 백테스트로 다시 정한다.
지금 기본값은 보수적인 자리표시자다. 처음엔 반드시 --scalp-budget 을 작게 두고 가상매매/소액으로 확인한다.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from .fmt import arrow, b, esc, i, pct, won
from .risk import position_size

log = logging.getLogger(__name__)

SCALP_COMMANDS = [("/scalp", "단타 상태"), ("/scalp_yes", "급등 알림 종목 단타 승인 (10분 안)"),
                  ("/scalp_no", "승인 대기 취소"), ("/scalp_off", "단타 전량 정리 후 끔"), ("/scalp_on", "단타 다시 켬")]


@dataclass
class ScalpConfig:
    budget_pct: float = 0.1  # 1회 매수 예산 (총자산 대비). 0 이면 단타 기능 끔
    take_profit: float = 0.03  # 매수가 대비 목표 수익
    stop_loss: float = 0.015  # 매수가 대비 손절
    trail: float = 0.015  # 진입 후 최고가 대비 하락 → 정리 (추적 손절)
    max_trades_per_day: int = 5
    max_daily_loss_pct: float = 0.02  # 하루 단타 손실 한도 (총자산 대비) → 그날 단타 종료
    close_time: dtime = dtime(15, 20)  # 이 시각 이후 보유분 강제 정리, 신규 매수 금지
    approve_timeout_sec: int = 600  # 알림 후 승인 유효 시간


class ScalpManager:
    def __init__(self, bot, cfg: ScalpConfig, data_dir: str | Path = "data"):
        self.bot = bot
        self.cfg = cfg
        self.log_path = Path(data_dir) / "scalp_trades.csv"
        # 재시작해도 유지되는 상태는 봇 상태 파일의 "scalp" 에 통째로 저장
        s = bot.state.scalp
        s.setdefault("enabled", True)
        s.setdefault("pending", {})  # 종목 -> {"expires": iso, "price": 알림 시 가격}
        s.setdefault("positions", {})  # 종목 -> {"qty","price","high","opened"}
        s.setdefault("day", "")
        s.setdefault("trades", 0)
        s.setdefault("pnl_today", 0.0)
        s.setdefault("stopped", "")  # 오늘 단타를 멈춘 이유 (비면 정상)

    # ---------- 상태 ----------
    @property
    def s(self) -> dict:
        return self.bot.state.scalp

    def _roll(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.s["day"] != today:
            self.s.update(day=today, trades=0, pnl_today=0.0, stopped="", pending={})
            self.bot.state.save()

    def _label(self, sym: str) -> str:
        return self.bot.label_of(sym)

    def describe(self) -> str:
        c = self.cfg
        return (f"⚡ 단타(승인형): 1회 예산 {c.budget_pct:.0%} · 목표 {c.take_profit:+.1%} · 손절 {-c.stop_loss:.1%}"
                f" · 고점 대비 {-c.trail:.1%} · 하루 {c.max_trades_per_day}회 · {c.close_time:%H:%M} 강제 정리")

    def status_html(self) -> str:
        on = "켜짐" if self.s["enabled"] else "꺼짐 (/scalp_on)"
        lines = [f"⚡ {b('단타')} {on}" + (f" · 오늘 중단: {esc(self.s['stopped'])}" if self.s["stopped"] else ""),
                 f"오늘 {self.s['trades']}회 · 손익 {arrow(self.s['pnl_today'])} {self.s['pnl_today']:+,.0f}원"]
        for sym, p in self.s["positions"].items():
            lines.append(f"보유 {esc(self._label(sym))} {p['price']:,.0f} × {p['qty']}주 (고점 {p['high']:,.0f})")
        if self.s["pending"]:
            lines.append("승인 대기: " + ", ".join(esc(self._label(x)) for x in self.s["pending"]))
        return "\n".join(lines)

    # ---------- 급등 알림 → 승인 대기 ----------
    def offer(self, symbol: str, price: float, now: datetime) -> str | None:
        """급등 알림 뒤에 붙일 승인 안내문. 단타를 제안할 수 없는 상황이면 None."""
        self._roll(now)
        if (not self.s["enabled"] or self.s["stopped"] or symbol in self.s["positions"]
                or self.s["trades"] >= self.cfg.max_trades_per_day or now.time() >= self.cfg.close_time):
            return None
        expires = now + timedelta(seconds=self.cfg.approve_timeout_sec)
        self.s["pending"][symbol] = {"expires": expires.isoformat(), "price": price}
        self.bot.state.save()
        return (f"⚡ 단타 들어갈까요? {b('/scalp_yes')} ({expires:%H:%M} 까지) · 취소 /scalp_no\n"
                f"{i(f'예산 총자산의 {self.cfg.budget_pct:.0%} · 목표 {self.cfg.take_profit:+.1%} · 손절 {-self.cfg.stop_loss:.1%}')}")

    def _expire(self, now: datetime) -> None:
        gone = [s for s, p in self.s["pending"].items() if datetime.fromisoformat(p["expires"]) <= now]
        for s in gone:
            self.s["pending"].pop(s, None)

    # ---------- 명령 ----------
    def command(self, cmd: str, now: datetime) -> None:
        self._roll(now)
        self._expire(now)
        bot = self.bot
        if cmd == "/scalp":
            bot.notify(self.status_html(), html=True)
        elif cmd == "/scalp_yes":
            if not self.s["pending"]:
                bot.notify("승인 대기 중인 급등 종목이 없습니다 (10분이 지났거나 알림이 없었음).")
                return
            # 가장 최근 알림 종목 하나만 승인
            sym = list(self.s["pending"])[-1]
            self.s["pending"].pop(sym)
            self.s["pending"].clear()
            self.enter(sym, now)
        elif cmd == "/scalp_no":
            self.s["pending"].clear()
            bot.state.save()
            bot.notify("단타 승인 대기를 취소했습니다.")
        elif cmd == "/scalp_off":
            self.s["enabled"] = False
            self.s["pending"].clear()
            n = self.close_all(now, "/scalp_off")
            bot.notify(f"⚡ 단타를 껐습니다. 정리 {n}종목. 다시 켜려면 /scalp_on")
        elif cmd == "/scalp_on":
            self.s["enabled"] = True
            bot.state.save()
            bot.notify("⚡ 단타를 켰습니다. 급등 알림이 오면 /scalp_yes 로 승인하세요.")

    # ---------- 매매 ----------
    def enter(self, symbol: str, now: datetime) -> bool:
        bot = self.bot
        if bot.state.paused:
            bot.notify("⛔ 봇이 일시정지 상태라 단타를 시작하지 않습니다 (/start 후 다시).")
            return False
        if now.time() >= self.cfg.close_time:
            bot.notify(f"{self.cfg.close_time:%H:%M} 이후에는 단타를 새로 시작하지 않습니다.")
            return False
        if not bot._can_order():
            return False
        acc = bot.broker.account()
        price = bot.broker.current_price(symbol)
        risk = replace(bot.cfg.risk, max_position_pct=self.cfg.budget_pct)
        qty = position_size(acc.equity, acc.cash, price, risk, bot.cfg.commission)
        if qty <= 0:
            bot.notify(f"⚡ {esc(self._label(symbol))}: 예산({won(acc.equity * self.cfg.budget_pct)})으로 1주도 살 수 없어 건너뜁니다.", html=True)
            return False
        res = bot.broker.buy(symbol, qty)
        bot.state.orders += 1
        if not res.ok:
            bot.notify_error(f"단타 매수 실패 {self._label(symbol)}: {res.message}")
            bot.state.save()
            return False
        self.s["positions"][symbol] = {"qty": res.qty, "price": price, "high": price, "opened": now.isoformat()}
        bot.state.save()
        bot.notify(f"⚡🔴 {b('단타 매수')} {esc(self._label(symbol))}\n"
                   f"약 {price:,.0f} × {res.qty} = 약 {won(res.qty * price)}\n"
                   f"목표 {price * (1 + self.cfg.take_profit):,.0f} · 손절 {price * (1 - self.cfg.stop_loss):,.0f}"
                   f" · {self.cfg.close_time:%H:%M} 강제 정리\n주문번호 {esc(res.order_id)}", html=True)
        return True

    def exit_reason(self, p: dict, price: float, now: datetime) -> str | None:
        entry = p["price"]
        if now.time() >= self.cfg.close_time:
            return "장 마감 전 정리"
        if price >= entry * (1 + self.cfg.take_profit):
            return f"목표 수익 {self.cfg.take_profit:+.1%} 도달"
        if price <= entry * (1 - self.cfg.stop_loss):
            return f"손절 {-self.cfg.stop_loss:.1%}"
        if p["high"] > entry and price <= p["high"] * (1 - self.cfg.trail):
            return f"고점 대비 {-self.cfg.trail:.1%} 하락"
        return None

    def close(self, symbol: str, price: float, reason: str, now: datetime) -> bool:
        bot = self.bot
        p = self.s["positions"].get(symbol)
        if not p or bot.state.paused:
            return False
        res = bot.broker.sell(symbol, p["qty"])
        bot.state.orders += 1
        if not res.ok:
            bot.notify_error(f"단타 매도 실패 {self._label(symbol)}: {res.message}")
            bot.state.save()
            return False
        qty = res.qty
        cost, proceeds = qty * p["price"], qty * price
        fee = cost * bot.cfg.commission + proceeds * (bot.cfg.commission + bot.cfg.sell_tax)
        pnl = proceeds - cost - fee
        if qty < p["qty"]:
            p["qty"] -= qty  # 일부만 접수됨 → 남은 수량은 다음 틱에 다시
        else:
            self.s["positions"].pop(symbol, None)
        self.s["trades"] += 1
        self.s["pnl_today"] += pnl
        bot.state.realized_today += pnl
        bot.state.realized_total += pnl
        self._log(now, symbol, p, qty, price, pnl, reason)
        bot.notify(f"⚡🔵 {b('단타 매도')} {esc(self._label(symbol))} · {esc(reason)}\n"
                   f"매수 {p['price']:,.0f} × {qty} = {won(cost)}\n매도 약 {price:,.0f} × {qty} = 약 {won(proceeds)}\n"
                   f"{arrow(pnl)} {b(f'{pnl:+,.0f}원')} ({pct(pnl / cost if cost else 0.0)}, 비용 포함)\n"
                   f"오늘 단타 {self.s['trades']}회 · 합계 {self.s['pnl_today']:+,.0f}원", html=True)
        limit = self.cfg.max_daily_loss_pct * self._equity_guess()
        if limit > 0 and self.s["pnl_today"] <= -limit and not self.s["stopped"]:
            self.s["stopped"] = f"하루 단타 손실 한도({-self.cfg.max_daily_loss_pct:.0%}) 도달"
            bot.notify(f"⚡⛔ {b('오늘 단타 종료')}: {esc(self.s['stopped'])}. 내일 자동으로 다시 켜집니다.", html=True)
        bot.state.save()
        return True

    def _equity_guess(self) -> float:
        try:
            return self.bot.broker.account().equity
        except Exception:
            return 0.0

    def close_all(self, now: datetime, reason: str) -> int:
        n = 0
        for sym in list(self.s["positions"]):
            try:
                price = self.bot.broker.current_price(sym)
            except Exception:
                price = self.s["positions"][sym]["price"]
            n += self.close(sym, price, reason, now)
        self.bot.state.save()
        return n

    def on_tick(self, now: datetime) -> None:
        """틱마다: 승인 만료 정리, 보유 단타 종목 종료 조건 점검."""
        self._roll(now)
        self._expire(now)
        if not self.s["positions"]:
            self.bot.state.save()
            return
        if self.bot.state.paused:
            if now.time() >= self.cfg.close_time and self.s.get("warned") != self.s["day"]:
                self.s["warned"] = self.s["day"]
                self.bot.notify("⚠️ 봇이 일시정지 상태라 단타 보유분을 정리하지 못했습니다. 나무 앱에서 직접 매도하거나 /start 하세요.")
            self.bot.state.save()
            return
        for sym in list(self.s["positions"]):
            p = self.s["positions"][sym]
            try:
                price = self.bot.broker.current_price(sym)
            except Exception as e:
                log.warning("단타 현재가 조회 실패 %s: %s", sym, e)
                continue
            p["high"] = max(p["high"], price)
            reason = self.exit_reason(p, price, now)
            if reason:
                self.close(sym, price, reason, now)
        self.bot.state.save()

    def _log(self, now: datetime, symbol: str, p: dict, qty: int, price: float, pnl: float, reason: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.log_path.exists()
            with self.log_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "symbol", "opened", "closed", "qty", "buy", "sell", "high", "pnl", "reason"])
                w.writerow([now.date().isoformat(), symbol, p["opened"][11:19], now.strftime("%H:%M:%S"), qty,
                            f"{p['price']:.0f}", f"{price:.0f}", f"{p['high']:.0f}", f"{pnl:.0f}", reason])
        except OSError as e:
            log.warning("단타 기록 실패: %s", e)
