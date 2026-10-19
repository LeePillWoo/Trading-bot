"""매매 전략.

각 전략은 일봉 OHLCV DataFrame(columns: open, high, low, close, volume)을 받아
날짜별 목표 포지션(1 = 보유, 0 = 미보유) Series를 반환한다.
t일 값은 t일 종가까지의 정보만 사용해야 한다 (미래 참조 금지).
"""
from __future__ import annotations

import pandas as pd


class Strategy:
    name = "base"
    # 신호 계산에 필요한 최소 봉 개수
    warmup = 1

    def signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError


class SmaCrossStrategy(Strategy):
    """단기 이동평균이 장기 이동평균 위에 있으면 보유."""

    name = "sma"

    def __init__(self, short: int = 5, long: int = 20):
        if short >= long:
            raise ValueError("short 기간은 long 기간보다 작아야 합니다")
        self.short, self.long = short, long
        self.warmup = long

    def signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        fast = close.rolling(self.short).mean()
        slow = close.rolling(self.long).mean()
        return (fast > slow).astype(int).where(slow.notna(), 0)


class RsiStrategy(Strategy):
    """RSI가 과매도(oversold) 아래로 내려가면 매수, 과매수(overbought) 위로 올라가면 매도."""

    name = "rsi"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period, self.oversold, self.overbought = period, oversold, overbought
        self.warmup = period + 1

    @staticmethod
    def rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = gain / loss
        return 100 - 100 / (1 + rs)

    def signals(self, df: pd.DataFrame) -> pd.Series:
        rsi = self.rsi(df["close"], self.period)
        pos, state = [], 0
        for value in rsi:
            if pd.notna(value):
                if value < self.oversold:
                    state = 1
                elif value > self.overbought:
                    state = 0
            pos.append(state)
        return pd.Series(pos, index=df.index, dtype=int)


class BreakoutStrategy(Strategy):
    """채널 돌파(터틀 방식): 종가가 N일 최고가를 돌파하면 매수, M일 최저가를 이탈하면 매도."""

    name = "breakout"

    def __init__(self, entry: int = 20, exit: int = 10):
        self.entry, self.exit = entry, exit
        self.warmup = max(entry, exit) + 1

    def signals(self, df: pd.DataFrame) -> pd.Series:
        upper = df["high"].rolling(self.entry).max().shift(1)
        lower = df["low"].rolling(self.exit).min().shift(1)
        pos, state = [], 0
        for close, up, lo in zip(df["close"], upper, lower):
            if pd.notna(up) and close > up:
                state = 1
            elif pd.notna(lo) and close < lo:
                state = 0
            pos.append(state)
        return pd.Series(pos, index=df.index, dtype=int)


STRATEGIES = {cls.name: cls for cls in (SmaCrossStrategy, RsiStrategy, BreakoutStrategy)}


def make_strategy(name: str, **params) -> Strategy:
    try:
        return STRATEGIES[name](**params)
    except KeyError:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {', '.join(STRATEGIES)})") from None
