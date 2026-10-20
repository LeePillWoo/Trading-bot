"""브로커 인터페이스."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    current_price: float = 0.0


@dataclass
class Account:
    cash: float  # 주문 가능 현금
    positions: dict[str, Position]
    # 증권사가 계산한 순자산 (있으면 이 값을 총자산으로 사용).
    # 결제 전(2영업일) 매도분이 현금과 잔고에 동시에 잡히는 이중 계산을 피하기 위함.
    total: float = 0.0

    @property
    def equity(self) -> float:
        if self.total > 0:
            return self.total
        return self.cash + sum(p.qty * p.current_price for p in self.positions.values())


@dataclass
class OrderResult:
    symbol: str
    side: str  # buy | sell
    qty: int
    price: float  # 0 이면 시장가
    order_id: str = ""
    ok: bool = True
    message: str = ""


class Broker(ABC):
    @abstractmethod
    def daily_ohlcv(self, symbol: str, days: int = 120) -> pd.DataFrame:
        """일봉 OHLCV (index: 날짜 오름차순, columns: open, high, low, close, volume)."""

    def investor_flow(self, symbol: str, days: int = 120) -> pd.DataFrame:
        """투자자별 일별 순매수량(주) (index: 날짜, columns: foreign, institution, individual, program)."""
        raise NotImplementedError(f"{type(self).__name__} 는 투자자 수급 데이터를 지원하지 않습니다")

    @abstractmethod
    def current_price(self, symbol: str) -> float: ...

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def buy(self, symbol: str, qty: int, price: float = 0) -> OrderResult: ...

    @abstractmethod
    def sell(self, symbol: str, qty: int, price: float = 0) -> OrderResult: ...
