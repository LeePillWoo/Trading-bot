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

    def explain(self, df: pd.DataFrame, target: int) -> str:
        """보고용: 마지막 봉 기준으로 왜 보유(1)/미보유(0)인지 한 줄 설명. 모르면 빈 문자열."""
        return ""

    def priority_series(self, df: pd.DataFrame) -> pd.Series:
        """매수 우선순위 (작을수록 먼저 산다). 같은 날 매수 신호가 여럿이고 현금이 모자랄 때 쓴다.
        기본은 모두 같음 = 후보 목록 순서."""
        return pd.Series(0.0, index=df.index)

    def outlook(self, df: pd.DataFrame) -> dict:
        """보유 종목 전망 (마감 보고 총평용). 마지막 봉까지 반영해 '다음 장 아침에 팔 것인지' 등을 돌려준다.
        키: sell_tomorrow(bool), why(str), rsi(float), room(손절선까지 여유, 비율) — 전략마다 있는 것만."""
        try:
            return {"sell_tomorrow": int(self.signals(df).iloc[-1]) == 0, "why": "매도 신호"}
        except Exception:
            return {}


def _md(ts) -> str:
    return f"{ts.month}/{ts.day}"


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

    def explain(self, df: pd.DataFrame, target: int) -> str:
        close = df["close"]
        f, sl = close.rolling(self.short).mean().iloc[-1], close.rolling(self.long).mean().iloc[-1]
        if pd.isna(sl):
            return ""
        rel = ">" if f > sl else "<"
        trend = "상승 추세" if f > sl else "추세 이탈"
        return f"{trend}: {self.short}일선 {f:,.0f} {rel} {self.long}일선 {sl:,.0f}"


class RsiStrategy(Strategy):
    """RSI가 과매도(oversold) 아래로 내려가면 매수, 과매수(overbought) 위로 올라가면 매도.

    상황 판단 손절 (guard > 0):
    과매도 매수는 '잠깐 빠졌다가 반등한다'에 거는 것이다. 그 판단이 틀린 신호가 나오면 판다.
    - 신호일 종가보다 guard(예 10%) 넘게 더 빠졌는데
      · 최근 flow_days 일 외국인+기관(연기금 포함)이 순매도 중이거나 (큰손 이탈)
      · 같은 기간 시장(코스피200)보다 guard/2 넘게 더 빠졌으면 (시장 탓이 아닌 종목 문제)
      → '바닥이 안 보이는 하락'으로 보고 매도.
    - 시장 전체가 같이 빠졌고 큰손이 사들이고 있으면 → 일시적 하락(기회)으로 보고 계속 보유.
    - 신호일 대비 floor(예 25%) 넘게 빠지면 이유 불문 매도 (최후 안전장치).
    손절 후에는 RSI가 50 위로 회복하기 전까지 다시 사지 않는다 (떨어지는 칼날 반복 매수 방지).
    시장 데이터는 df["mkt"] (코스피200 추종 지수 종가), 수급은 df["foreign"], df["institution"].
    """

    name = "rsi"
    REARM = 50  # 손절 후 재진입 허용 RSI

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70,
                 rank: int = 1, flow: int = 0, flow_days: int = 5, guard: float = 0.0, floor: float = 0.25):
        self.period, self.oversold, self.overbought = period, oversold, overbought
        self.warmup = period + 1
        # 매수 우선순위: rank=1 이면 RSI 낮은(많이 빠진) 종목부터. flow=1 이면 최근 flow_days 일
        # 외국인+기관(연기금 포함) 순매수가 플러스인 종목을 먼저, 그 안에서 RSI 낮은 순.
        self.rank, self.flow, self.flow_days = rank, flow, flow_days
        self.guard, self.floor = guard, floor
        self.needs_flow = bool(flow or guard)

    def flow_sum(self, df: pd.DataFrame) -> pd.Series:
        if not {"foreign", "institution"} <= set(df.columns):
            return pd.Series(float("nan"), index=df.index)
        return (df["foreign"] + df["institution"]).rolling(self.flow_days).sum()

    def priority_series(self, df: pd.DataFrame) -> pd.Series:
        if not self.rank:
            return super().priority_series(df)
        score = self.rsi(df["close"], self.period).fillna(100.0)
        if self.flow:
            score = score + (self.flow_sum(df) > 0).map({True: 0.0, False: 100.0})
        return score

    @staticmethod
    def rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = gain / loss
        return 100 - 100 / (1 + rs)

    def _walk(self, df: pd.DataFrame) -> tuple[list[int], dict]:
        """봉마다 보유 상태를 계산. 마지막 봉의 판단 근거(info)도 같이 돌려준다 (보고용)."""
        rsi = self.rsi(df["close"], self.period)
        close = df["close"].to_numpy(dtype=float)
        mkt = df["mkt"].to_numpy(dtype=float) if "mkt" in df.columns else None
        flow = self.flow_sum(df).to_numpy(dtype=float)
        pos, state, armed = [], 0, True
        entry = info = cut_info = None
        for k, value in enumerate(rsi):
            info = None
            if pd.notna(value):
                if state == 0:
                    if not armed and value > self.REARM:
                        armed, cut_info = True, None
                    if armed and value < self.oversold:
                        state, entry, cut_info = 1, k, None
                elif value > self.overbought:
                    state = 0
                elif self.guard:
                    drop = close[k] / close[entry] - 1
                    mdrop = mkt[k] / mkt[entry] - 1 if mkt is not None and mkt[entry] > 0 else float("nan")
                    info = {"entry": df.index[entry], "date": df.index[k], "drop": drop, "mdrop": mdrop,
                            "flow": flow[k]}
                    cut = ""
                    if drop <= -self.floor:
                        cut = "floor"
                    elif drop <= -self.guard:
                        if flow[k] < 0:
                            cut = "flow"
                        elif drop - mdrop <= -self.guard / 2:
                            cut = "relative"
                    if cut:
                        info["cut"] = cut
                        state, armed, cut_info = 0, False, info
            pos.append(state)
        # 보유 중이면 오늘 판단 근거, 손절로 나온 뒤 아직 재진입 대기 중이면 그 손절 근거를 돌려준다
        return pos, info or cut_info or {}

    def signals(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(self._walk(df)[0], index=df.index, dtype=int)

    def outlook(self, df: pd.DataFrame) -> dict:
        rsi = self.rsi(df["close"], self.period).dropna()
        if rsi.empty:
            return {}
        now = float(rsi.iloc[-1])
        pos, info = self._walk(df)
        out = {"rsi": now, "sell_tomorrow": pos[-1] == 0}
        if pos[-1] == 0:
            if info.get("cut"):
                out["why"] = {"floor": "최후 안전장치 손절", "flow": "큰손 이탈로 손절",
                              "relative": "시장 대비 급락으로 손절"}[info["cut"]]
            elif now > self.overbought:
                out["why"] = f"과매수 RSI {now:.0f}, 차익 실현"
            else:
                out["why"] = "매도 신호"
        elif info and self.guard:
            out["room"] = max(0.0, info["drop"] + self.guard)  # 손절선(-guard)까지 남은 여유
        return out

    def guard_note(self, info: dict) -> str:
        if not info:
            return ""
        parts = [f"{_md(info['entry'])} 신호 이후 {info['drop']:+.1%}"]
        if pd.notna(info["mdrop"]):
            parts.append(f"시장 {info['mdrop']:+.1%}")
        if pd.notna(info["flow"]):
            parts.append(f"외국인+기관 {self.flow_days}일 {info['flow']:+,.0f}주")
        return " · ".join(parts)

    def explain(self, df: pd.DataFrame, target: int) -> str:
        rsi = self.rsi(df["close"], self.period).dropna()
        if rsi.empty:
            return ""
        now = rsi.iloc[-1]
        _, info = self._walk(df)
        if info.get("cut"):
            why = {"floor": f"신호일 대비 {self.floor:.0%} 넘게 하락 (최후 안전장치)",
                   "flow": "더 빠지는데 외국인+기관이 계속 팔고 있음 (큰손 이탈)",
                   "relative": "시장보다 훨씬 더 빠짐 (시장 탓이 아닌 종목 문제)"}[info["cut"]]
            when = f" · {_md(info['date'])} 종가 기준 판단" if info["date"] != rsi.index[-1] else ""
            return (f"상황 판단 손절: {why} · {self.guard_note(info)}{when}"
                    f" · 재매수는 RSI {self.REARM} 회복 후 (어제 RSI {now:.0f})")
        if target:
            low = rsi[rsi < self.oversold]
            when = f"{_md(low.index[-1])} RSI {low.iloc[-1]:.0f}로 과매도({self.oversold:g} 아래) → 반등 노림" if len(low) else ""
            text = f"{when} · 어제 RSI {now:.0f}".strip(" ·")
            if info:
                text += f" · {self.guard_note(info)}"
                if info["drop"] <= -self.guard:
                    text += " → 시장과 같이 빠졌고 큰손 매수 중: 일시적 하락으로 보고 보유"
            else:
                fl = self.flow_sum(df).iloc[-1]
                if pd.notna(fl):
                    text += f" · 외국인+기관 {self.flow_days}일 {fl:+,.0f}주{' (순매수)' if fl > 0 else ''}"
            return text
        high = rsi[rsi > self.overbought]
        if len(high) and high.index[-1] == rsi.index[-1]:
            return f"과매수: 어제 RSI {now:.0f} ({self.overbought:g} 위) → 차익 실현"
        when = f" ({_md(high.index[-1])} RSI {high.iloc[-1]:.0f} 과매수)" if len(high) else ""
        return f"과매수 신호 후 미보유{when} · 어제 RSI {now:.0f}"


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

    def explain(self, df: pd.DataFrame, target: int) -> str:
        up = df["high"].rolling(self.entry).max().shift(1).iloc[-1]
        lo = df["low"].rolling(self.exit).min().shift(1).iloc[-1]
        c = df["close"].iloc[-1]
        if target:
            return f"{self.entry}일 최고가 돌파 후 추세 유지 · 어제 종가 {c:,.0f} (이탈선 {lo:,.0f})"
        return f"{self.exit}일 최저가 이탈 · 어제 종가 {c:,.0f} < {lo:,.0f}" if c < lo else \
            f"돌파 대기 · 어제 종가 {c:,.0f} (돌파선 {up:,.0f})"


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

    def explain(self, df: pd.DataFrame, target: int) -> str:
        base = SmaCrossStrategy(self.short, self.long).explain(df, target)
        fl = self.flow_sum(df).iloc[-1]
        return f"{base} · 최근 {self.flow_days}일 외국인+기관 {fl:+,.0f}주" if pd.notna(fl) else base


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

    def reasons(self, closes: dict[str, pd.Series]) -> dict[str, str]:
        """보고용: 종목별 선택/제외 이유."""
        mom = self.momentum(closes)
        ranked = sorted(mom, key=mom.get, reverse=True)
        hurdle = (1 + self.rf) ** (self.lookback / 252) - 1
        market = self.market_ok(closes)
        out = {}
        for k, sym in enumerate(ranked, 1):
            m = f"최근 {self.lookback}거래일 {mom[sym]:+.1%} · {len(ranked)}종목 중 {k}위"
            if not market:
                out[sym] = f"{m} · 시장 필터(하락장) → 전부 현금"
            elif k > self.top_n:
                out[sym] = f"{m} (상위 {self.top_n} 밖)"
            elif mom[sym] <= hurdle:
                out[sym] = f"{m} · 수익률이 기준({hurdle:+.1%}) 이하"
            else:
                out[sym] = f"{m} (상위 {self.top_n})"
        return out

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
