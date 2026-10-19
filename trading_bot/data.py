"""백테스트용 데이터 로딩."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]
# 한글/영문 컬럼명 지원
ALIASES = {
    "date": "date", "날짜": "date", "일자": "date",
    "open": "open", "시가": "open",
    "high": "high", "고가": "high",
    "low": "low", "저가": "low",
    "close": "close", "종가": "close", "adj close": "adj_close",
    "volume": "volume", "거래량": "volume",
}


def load_csv(path: str | Path) -> pd.DataFrame:
    """date, open, high, low, close, volume 컬럼을 가진 CSV를 읽는다."""
    df = pd.read_csv(path)
    df = df.rename(columns={c: ALIASES.get(c.strip().lower(), c) for c in df.columns})
    missing = {"date", *COLUMNS[:4]} - set(df.columns)
    if missing:
        raise ValueError(f"{path}: 필요한 컬럼이 없습니다: {sorted(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0
    df["date"] = pd.to_datetime(df["date"].astype(str))
    return df.set_index("date").sort_index()[COLUMNS].astype(float)


def synthetic_ohlcv(days: int = 500, start_price: float = 50_000, seed: int = 0,
                    drift: float = 0.0003, vol: float = 0.02) -> pd.DataFrame:
    """테스트/데모용 랜덤워크 일봉 데이터."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=days)
    close = start_price * np.exp(np.cumsum(rng.normal(drift, vol, days)))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, vol / 4, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, days)))
    volume = rng.integers(100_000, 1_000_000, days).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                        index=dates)
