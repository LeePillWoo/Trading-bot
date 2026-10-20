"""장중 시세 기록 + 급등 감지 (알림만, 매매 없음).

나중에 단타 전략을 만들려면 분 단위 시세가 필요한데 증권사는 과거 분봉을 오래 주지 않는다.
그래서 봇이 장중에 관심 종목의 현재가와 누적 거래량을 주기적으로 파일에 쌓아 둔다.

- 기록: data/ticks/YYYY-MM-DD.csv (time,symbol,price,volume). 하루 한 파일, 봇이 켜져 있는 동안만.
- 급등 감지: 오늘 누적 거래량이 '최근 20일 평균 거래량 × 지금까지 지난 장 시간 비율' 의 vol_ratio 배를
  넘고, 전일 종가 대비 min_change 이상 올랐으면 텔레그램으로 알린다 (종목당 하루 한 번).
  어떤 주문도 내지 않는다. 매매는 사람이 판단한다.

시세는 일봉 API(오늘 행 = 장중 현재가·누적 거래량)로 받는다. 종목당 호출 1회로 기준(20일 평균)까지
한 번에 얻을 수 있고, 모의투자 모드에서도 동작한다.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

from .broker.base import Broker

log = logging.getLogger(__name__)

SESSION_MINUTES = 390  # 09:00 ~ 15:30
MIN_ELAPSED = 10  # 장 시작 직후는 거래량 비율이 튀므로 10분은 지난 것으로 계산
BASELINE_DAYS = 20


@dataclass
class Spike:
    symbol: str
    price: float
    change: float  # 전일 종가 대비
    vol_ratio: float  # 평소 같은 시간대 거래량 대비 배수


class TickWatcher:
    def __init__(self, broker: Broker, symbols: list[str], data_dir: str | Path = "data/ticks",
                 vol_ratio: float = 5.0, min_change: float = 0.03):
        self.broker = broker
        self.symbols = list(dict.fromkeys(symbols))  # 순서 유지, 중복 제거
        self.data_dir = Path(data_dir)
        self.vol_ratio = vol_ratio
        self.min_change = min_change
        self.alerted: dict[str, set[str]] = {}  # 날짜 -> 오늘 이미 알린 종목

    def describe(self) -> str:
        return (f"관심 {len(self.symbols)}종목 기록 · 급등 알림: 거래량 평소 {self.vol_ratio:g}배 이상"
                f" + 전일 대비 {self.min_change:+.0%} 이상")

    def poll(self, now: datetime) -> list[Spike]:
        """관심 종목을 한 바퀴 돌며 기록하고, 새로 감지된 급등만 돌려준다."""
        today = now.date()
        path = self.data_dir / f"{today.isoformat()}.csv"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        alerted = self.alerted.setdefault(today.isoformat(), set())
        spikes = []
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "symbol", "price", "volume"])
            for sym in self.symbols:
                try:
                    df = self.broker.daily_ohlcv(sym, BASELINE_DAYS + 1)
                except Exception as e:
                    log.warning("시세 기록 실패 %s: %s", sym, e)
                    continue
                if df.empty or df.index[-1].date() != today:
                    continue  # 오늘 데이터가 없으면 휴장 (또는 아직 집계 전)
                row = df.iloc[-1]
                price, vol = float(row["close"]), float(row["volume"])
                w.writerow([now.strftime("%H:%M:%S"), sym, f"{price:.0f}", f"{vol:.0f}"])
                prev = df.iloc[:-1]
                if sym in alerted or len(prev) < 5:
                    continue
                spike = self._detect(sym, now, price, vol, float(prev["close"].iloc[-1]),
                                     float(prev["volume"].mean()))
                if spike:
                    alerted.add(sym)
                    spikes.append(spike)
        return spikes

    def _detect(self, sym: str, now: datetime, price: float, vol: float,
                prev_close: float, avg_vol: float) -> Spike | None:
        if prev_close <= 0 or avg_vol <= 0:
            return None
        elapsed = (now.hour - 9) * 60 + now.minute
        elapsed = min(max(elapsed, MIN_ELAPSED), SESSION_MINUTES)
        expected = avg_vol * elapsed / SESSION_MINUTES
        ratio = vol / expected
        change = price / prev_close - 1
        if ratio >= self.vol_ratio and change >= self.min_change:
            return Spike(sym, price, change, ratio)
        return None
