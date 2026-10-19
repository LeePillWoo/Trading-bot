"""일봉 백테스트.

- t일 종가로 신호를 계산하고 t+1일 시가에 체결한다 (미래 참조 방지).
- 손절/익절은 t일 종가 기준으로 판단해 t+1일 시가에 매도한다.
- 손절 후에는 신호가 한 번 0이 될 때까지 재진입하지 않는다 (실거래 봇과 동일한 규칙).
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
                 sell_tax: float = 0.002) -> BacktestResult:
    risk = risk or RiskConfig()
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
    # 벤치마크: 전 종목 동일비중 단순보유
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).reindex(equity.index).ffill().bfill()
    benchmark = (closes / closes.iloc[0]).mean(axis=1) * equity.iloc[0]
    return BacktestResult(equity=equity, trades=trades, benchmark=benchmark)
