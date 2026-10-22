"""매매 전략.

각 전략은 일봉 OHLCV DataFrame(columns: open, high, low, close, volume)을 받아
날짜별 목표 포지션(1 = 보유, 0 = 미보유) Series를 반환한다.
t일 값은 t일 종가까지의 정보만 사용해야 한다 (미래 참조 금지).

needs_flow = True 인 전략은 투자자별 순매수 컬럼(foreign, institution)도 필요하다
(with_flow 로 붙인다).
"""
from __future__ import annotations

import pandas as pd


FLOW_COLUMNS = ["foreign", "institution", "individual", "program"]


def with_flow(df: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    """일봉에 투자자별 순매수(주 단위)를 날짜 기준으로 붙인다. 없는 날은 NaN."""
    cols = [c for c in FLOW_COLUMNS if c in flow.columns]
    return df.drop(columns=[c for c in cols if c in df.columns]).join(flow[cols], how="left")


class Strategy:
    name = "base"
    # 신호 계산에 필요한 최소 봉 개수
    warmup = 1
    needs_flow = False

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


class SmaFlowStrategy(Strategy):
    """이동평균 + 수급 필터 (추천).

    매수: 단기 이평 > 장기 이평 (상승 추세) 이고, 최근 flow_days 일 외국인+기관 순매수 합계 > 0
    매도: 단기 이평 < 장기 이평 (추세 이탈). 수급은 진입 조건으로만 쓴다.
    """

    name = "sma_flow"
    needs_flow = True

    def __init__(self, short: int = 5, long: int = 20, flow_days: int = 5):
        if short >= long:
            raise ValueError("short 기간은 long 기간보다 작아야 합니다")
        self.short, self.long, self.flow_days = short, long, flow_days
        self.warmup = max(long, flow_days)

    def flow_sum(self, df: pd.DataFrame) -> pd.Series:
        """최근 flow_days 일 외국인+기관 순매수 합계 (데이터가 빠진 날이 있으면 NaN)."""
        if not {"foreign", "institution"} <= set(df.columns):
            raise ValueError("sma_flow 전략은 투자자 수급 데이터가 필요합니다 (나무증권 API)")
        return (df["foreign"] + df["institution"]).rolling(self.flow_days).sum()

    def signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        fast, slow = close.rolling(self.short).mean(), close.rolling(self.long).mean()
        flow = self.flow_sum(df)
        pos, state = [], 0
        for f, sl, fl in zip(fast, slow, flow):
            if pd.isna(sl):
                state = 0
            elif state == 0 and f > sl and pd.notna(fl) and fl > 0:
                state = 1
            elif state == 1 and f < sl:
                state = 0
            pos.append(state)
        return pd.Series(pos, index=df.index, dtype=int)


class VolatilityBreakoutStrategy(Strategy):
    """변동성 돌파 (래리 윌리엄스). 장중 매매 전략 — 백테스트 전용.

    매수: 오늘 시가 + 어제(고가-저가) × k 를 장중에 넘는 순간, 그 가격에
    매도: 다음 날 시가에 전량
    ma > 0 이면 시가가 어제까지의 ma일 이동평균보다 높을 때만 매수 (하락장 필터)
    """

    name = "vb"
    intraday = True

    def __init__(self, k: float = 0.5, ma: int = 0):
        self.k, self.ma = k, ma
        self.warmup = max(ma, 1) + 1

    def entry_prices(self, df: pd.DataFrame) -> pd.Series:
        """날짜별 매수 가격 (매수 안 하는 날은 NaN). 어제까지의 값과 오늘 시가만 사용."""
        target = df["open"] + (df["high"] - df["low"]).shift(1) * self.k
        ok = df["high"] >= target
        if self.ma:
            ok &= df["open"] > df["close"].rolling(self.ma).mean().shift(1)
        return target.where(ok)

    def signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError("vb 는 장중 전략이라 일봉 신호가 없습니다 (백테스트 전용)")


class DualMomentumStrategy(Strategy):
    """듀얼 모멘텀 (월 1회 리밸런싱).

    매달 첫 거래일, 후보 종목 중 최근 lookback 거래일 수익률 상위 top_n 종목을 고르되
    (상대 모멘텀) 그 수익률이 무위험 수익률(rf, 연율)보다 높은 종목만 보유한다 (절대 모멘텀).
    조건을 만족하는 종목이 없으면 현금. 종목당 비중은 1/top_n.
    """

    name = "dual_momentum"
    portfolio = True
    every = "month"  # 리밸런싱 주기: month | week

    def __init__(self, lookback: int = 252, top_n: int = 2, rf: float = 0.03, market_ma: int = 0):
        self.lookback, self.top_n, self.rf = lookback, top_n, rf
        # 시장 필터: 후보 중 market_ma 일 이동평균 위에 있는 종목이 절반 미만이면 하락장으로 보고 전부 현금 (0 이면 끔)
        self.market_ma = market_ma
        self.warmup = max(lookback, market_ma) + 1

    def period_key(self, ts) -> str:
        """리밸런싱 주기 구분 키. 키가 바뀐 첫 거래일에 리밸런싱한다."""
        if self.every == "week":
            y, w, _ = ts.isocalendar()
            return f"{y}-W{w:02d}"
        return f"{ts.year}-{ts.month:02d}"

    @property
    def period_label(self) -> str:
        return {"week": "주", "month": "달"}[self.every]

    @property
    def weight(self) -> float:
        return 1 / self.top_n

    def momentum(self, closes: dict[str, pd.Series]) -> dict[str, float]:
        return {s: float(c.iloc[-1] / c.iloc[-1 - self.lookback] - 1)
                for s, c in closes.items() if len(c) > self.lookback}

    def market_ok(self, closes: dict[str, pd.Series]) -> bool:
        """후보 종목의 절반 이상이 market_ma 일 이동평균 위에 있으면 상승장(매수 허용)."""
        if not self.market_ma:
            return True
        above = [float(c.iloc[-1]) > float(c.iloc[-self.market_ma:].mean())
                 for c in closes.values() if len(c) >= self.market_ma]
        return not above or sum(above) * 2 >= len(above)

    def select(self, closes: dict[str, pd.Series]) -> list[str]:
        if not self.market_ok(closes):
            return []
        hurdle = (1 + self.rf) ** (self.lookback / 252) - 1
        mom = self.momentum(closes)
        ranked = sorted(mom, key=mom.get, reverse=True)[:self.top_n]
        return [s for s in ranked if mom[s] > hurdle]

    def signals(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError("dual_momentum 은 종목 묶음 단위 전략입니다 (select 사용)")


class RotationStrategy(DualMomentumStrategy):
    """주간 모멘텀 로테이션: 매주 첫 거래일, 최근 lookback(기본 60) 거래일 수익률 상위 top_n 종목을 보유.

    듀얼 모멘텀과 같은 규칙을 더 짧은 주기·기간으로 돌린다. 후보가 많을수록(10~20개) 주마다 교체가 생긴다.
    상위 종목도 수익률이 rf 이하이면 그 몫은 현금 → 시장 전체가 빠질 때는 알아서 현금으로 피한다.
    """

    name = "rotation"
    every = "week"

    def __init__(self, lookback: int = 60, top_n: int = 2, rf: float = 0.03, market_ma: int = 0):
        super().__init__(lookback, top_n, rf, market_ma)


STRATEGIES = {cls.name: cls for cls in (SmaFlowStrategy, SmaCrossStrategy, RsiStrategy, BreakoutStrategy,
                                        VolatilityBreakoutStrategy, DualMomentumStrategy, RotationStrategy)}


def make_strategy(name: str, **params) -> Strategy:
    try:
        return STRATEGIES[name](**params)
    except KeyError:
        raise ValueError(f"알 수 없는 전략: {name} (사용 가능: {', '.join(STRATEGIES)})") from None
