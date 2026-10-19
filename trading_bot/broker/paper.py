"""로컬 가상 브로커: 실제 주문 없이 시세 데이터만으로 체결을 시뮬레이션한다.

시세는 다른 브로커(예: KIS)나 미리 준비된 DataFrame에서 가져온다.
"""
from __future__ import annotations

import itertools

import pandas as pd

from .base import Account, Broker, OrderResult, Position


class PaperBroker(Broker):
    def __init__(self, cash: float = 10_000_000, data_source: Broker | None = None,
                 prices: dict[str, pd.DataFrame] | None = None,
                 commission: float = 0.00015, sell_tax: float = 0.002):
        self.cash = float(cash)
        self.positions: dict[str, Position] = {}
        self.data_source = data_source
        self.prices = prices or {}
        self.commission = commission
        self.sell_tax = sell_tax
        self._ids = itertools.count(1)

    def daily_ohlcv(self, symbol: str, days: int = 120) -> pd.DataFrame:
        if symbol in self.prices:
            return self.prices[symbol].tail(days)
        if self.data_source is None:
            raise KeyError(f"{symbol} 시세 데이터가 없습니다")
        return self.data_source.daily_ohlcv(symbol, days)

    def current_price(self, symbol: str) -> float:
        if symbol in self.prices:
            return float(self.prices[symbol]["close"].iloc[-1])
        if self.data_source is None:
            raise KeyError(f"{symbol} 시세 데이터가 없습니다")
        return self.data_source.current_price(symbol)

    def account(self) -> Account:
        for sym, pos in self.positions.items():
            pos.current_price = self.current_price(sym)
        return Account(cash=self.cash, positions=dict(self.positions))

    def buy(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        fill = price or self.current_price(symbol)
        cost = fill * qty * (1 + self.commission)
        if qty <= 0 or cost > self.cash:
            return OrderResult(symbol, "buy", qty, price, ok=False, message="현금 부족 또는 잘못된 수량")
        self.cash -= cost
        pos = self.positions.get(symbol)
        if pos:
            total = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + fill * qty) / total
            pos.qty = total
        else:
            self.positions[symbol] = Position(symbol, qty, fill, fill)
        return OrderResult(symbol, "buy", qty, fill, order_id=f"P{next(self._ids)}")

    def sell(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        pos = self.positions.get(symbol)
        if not pos or qty <= 0 or qty > pos.qty:
            return OrderResult(symbol, "sell", qty, price, ok=False, message="보유 수량 부족")
        fill = price or self.current_price(symbol)
        self.cash += fill * qty * (1 - self.commission - self.sell_tax)
        pos.qty -= qty
        if pos.qty == 0:
            del self.positions[symbol]
        return OrderResult(symbol, "sell", qty, fill, order_id=f"P{next(self._ids)}")
