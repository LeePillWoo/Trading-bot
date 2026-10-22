"""일봉 백테스트.

- t일 종가로 신호를 계산하고 t+1일 시가에 체결한다 (미래 참조 방지).
- 손절/익절은 t일 종가 기준으로 판단해 t+1일 시가에 매도한다.
- 손절 후에는 신호가 한 번 0이 될 때까지 재진입하지 않는다 (실거래 봇과 동일한 규칙).
- 변동성 돌파(vb): 장중 목표가 도달 시 그 가격에 매수, 다음 날 시가에 매도.
- 듀얼 모멘텀: 매달 첫 거래일 시가에, 전날 종가까지의 수익률로 고른 종목으로 교체.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import RiskConfig
from .risk import exit_reason, position_size
from .strategy import Strategy


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    reason: str = ""

    @property
    def pnl_pct(self) -> float | None:
        if self.exit_price is None:
            return None
        return self.exit_price / self.entry_price - 1


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade] = field(default_factory=list)
    benchmark: pd.Series | None = None

    def stats(self) -> dict:
        eq = self.equity
        rets = eq.pct_change().dropna()
        years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
        total = eq.iloc[-1] / eq.iloc[0] - 1
        closed = [t for t in self.trades if t.exit_price is not None]
        wins = [t for t in closed if t.pnl_pct > 0]
        out = {
            "기간": f"{eq.index[0].date()} ~ {eq.index[-1].date()}",
            "최종자산": round(float(eq.iloc[-1])),
            "총수익률": total,
            "연환산수익률(CAGR)": (1 + total) ** (1 / years) - 1,
            "최대낙폭(MDD)": float((eq / eq.cummax() - 1).min()),
            "샤프지수": float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0,
            "거래횟수": len(closed),
            "승률": len(wins) / len(closed) if closed else 0.0,
        }
        if self.benchmark is not None:
            out["단순보유수익률"] = float(self.benchmark.iloc[-1] / self.benchmark.iloc[0] - 1)
            out["단순보유MDD"] = float((self.benchmark / self.benchmark.cummax() - 1).min())
        return out


def run_backtest(data: dict[str, pd.DataFrame], strategy: Strategy, cash: float = 10_000_000,
                 risk: RiskConfig | None = None, commission: float = 0.00015,
                 sell_tax: float = 0.002, slippage: float = 0.0) -> BacktestResult:
    """slippage: 체결 불리함(매수는 그만큼 비싸게, 매도는 싸게). 매매 비용에 더해 계산."""
    risk = risk or RiskConfig()
    commission = commission + slippage
    if getattr(strategy, "intraday", False):
        return _run_intraday(data, strategy, cash, risk, commission, sell_tax)
    if getattr(strategy, "portfolio", False):
        return _run_portfolio(data, strategy, cash, commission, sell_tax)
    return _run_daily(data, strategy, cash, risk, commission, sell_tax)


def _benchmark(data: dict[str, pd.DataFrame], index: pd.DatetimeIndex, start: float) -> pd.Series:
    """전 종목 동일비중 단순보유."""
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).reindex(index).ffill().bfill()
    return (closes / closes.iloc[0]).mean(axis=1) * start


def _run_daily(data, strategy, cash, risk, commission, sell_tax) -> BacktestResult:
    signals = {s: strategy.signals(df) for s, df in data.items()}
    dates = sorted(set().union(*(df.index for df in data.values())))

    positions: dict[str, Trade] = {}
    blocked: set[str] = set()  # 손절/익절 후 재진입 대기
    pending: dict[str, str] = {}  # 다음 시가에 실행할 주문: symbol -> "buy" | "sell:<사유>"
    trades: list[Trade] = []
    equity_curve = []
    last_close: dict[str, float] = {}

    for date in dates:
        # 1) 전일 결정된 주문을 오늘 시가에 체결 (매도 먼저 처리해 현금 확보)
        prev_equity = cash + sum(t.qty * last_close.get(s, t.entry_price) for s, t in positions.items())
        for sym, action in sorted(pending.items(), key=lambda kv: not kv[1].startswith("sell")):
            df = data[sym]
            if date not in df.index:
                continue
            open_price = float(df.at[date, "open"])
            if action.startswith("sell") and sym in positions:
                t = positions.pop(sym)
                cash += open_price * t.qty * (1 - commission - sell_tax)
                t.exit_date, t.exit_price, t.reason = date, open_price, action.split(":", 1)[1]
            elif action == "buy" and sym not in positions:
                qty = position_size(prev_equity, cash, open_price, risk, commission)
                if qty > 0:
                    cash -= open_price * qty * (1 + commission)
                    t = Trade(sym, date, open_price, qty)
                    positions[sym] = t
                    trades.append(t)
            pending.pop(sym)

        # 2) 오늘 종가로 다음날 주문 결정
        for sym, df in data.items():
            if date not in df.index:
                continue
            close = float(df.at[date, "close"])
            last_close[sym] = close
            target = int(signals[sym].at[date])
            if target == 0:
                blocked.discard(sym)
            if sym in positions:
                reason = exit_reason(positions[sym].entry_price, close, risk)
                if reason:
                    pending[sym] = f"sell:{reason}"
                    blocked.add(sym)
                elif target == 0:
                    pending[sym] = "sell:신호"
            elif target == 1 and sym not in blocked:
                pending[sym] = "buy"

        equity_curve.append(cash + sum(t.qty * last_close[s] for s, t in positions.items()))

    equity = pd.Series(equity_curve, index=pd.DatetimeIndex(dates), name="equity")
    return BacktestResult(equity=equity, trades=trades, benchmark=_benchmark(data, equity.index, equity.iloc[0]))


def _run_intraday(data, strategy, cash, risk, commission, sell_tax) -> BacktestResult:
    """변동성 돌파: 목표가에 매수(종목당 max_position 비중), 다음 거래일 시가에 전량 매도."""
    entries = {s: strategy.entry_prices(df) for s, df in data.items()}
    dates = sorted(set().union(*(df.index for df in data.values())))
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    last_close: dict[str, float] = {}
    equity_curve = []
    for date in dates:
        # 1) 어제 산 종목은 오늘 시가에 매도
        for sym in list(positions):
            if date in data[sym].index:
                t = positions.pop(sym)
                price = float(data[sym].at[date, "open"])
                cash += price * t.qty * (1 - commission - sell_tax)
                t.exit_date, t.exit_price, t.reason = date, price, "익일 시가"
        equity = cash + sum(t.qty * last_close.get(s, t.entry_price) for s, t in positions.items())
        # 2) 오늘 목표가 돌파 종목 매수
        for sym, df in data.items():
            if date not in df.index or sym in positions:
                continue
            price = entries[sym].get(date)
            if price is not None and pd.notna(price):
                qty = position_size(equity, cash, float(price), risk, commission)
                if qty > 0:
                    cash -= float(price) * qty * (1 + commission)
                    t = Trade(sym, date, float(price), qty)
                    positions[sym] = t
                    trades.append(t)
        for sym, df in data.items():
            if date in df.index:
                last_close[sym] = float(df.at[date, "close"])
        equity_curve.append(cash + sum(t.qty * last_close[s] for s, t in positions.items()))
    equity = pd.Series(equity_curve, index=pd.DatetimeIndex(dates), name="equity")
    return BacktestResult(equity=equity, trades=trades, benchmark=_benchmark(data, equity.index, equity.iloc[0]))


def _run_portfolio(data, strategy, cash, commission, sell_tax) -> BacktestResult:
    """듀얼 모멘텀/로테이션: 달(주)이 바뀐 첫 거래일 시가에 리밸런싱 (선택은 전날 종가까지의 데이터로)."""
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index()
    opens = pd.DataFrame({s: df["open"] for s, df in data.items()}).reindex(closes.index)
    dates = closes.index
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    equity_curve = []
    last_close: dict[str, float] = {}
    for i, date in enumerate(dates):
        if i > 0 and strategy.period_key(date) != strategy.period_key(dates[i - 1]):
            hist = closes.iloc[:i]
            selected = strategy.select({s: hist[s].dropna() for s in hist.columns})
            # 빠지는 종목 매도
            for sym in [s for s in positions if s not in selected]:
                price = opens.at[date, sym]
                if pd.isna(price):
                    continue
                t = positions.pop(sym)
                cash += float(price) * t.qty * (1 - commission - sell_tax)
                t.exit_date, t.exit_price, t.reason = date, float(price), "리밸런싱"
            # 새로 들어오는 종목 매수 (전날 종가 기준 총자산 × 1/top_n)
            equity = cash + sum(t.qty * last_close.get(s, t.entry_price) for s, t in positions.items())
            for sym in selected:
                price = opens.at[date, sym]
                if sym in positions or pd.isna(price):
                    continue
                budget = min(equity * strategy.weight, cash)
                qty = int(budget // (float(price) * (1 + commission)))
                if qty > 0:
                    cash -= float(price) * qty * (1 + commission)
                    t = Trade(sym, date, float(price), qty)
                    positions[sym] = t
                    trades.append(t)
        for sym in closes.columns:
            if pd.notna(closes.at[date, sym]):
                last_close[sym] = float(closes.at[date, sym])
        equity_curve.append(cash + sum(t.qty * last_close[s] for s, t in positions.items()))
    equity = pd.Series(equity_curve, index=dates, name="equity")
    return BacktestResult(equity=equity, trades=trades, benchmark=_benchmark(data, equity.index, equity.iloc[0]))
