"""포지션 사이징과 손절/익절 판단."""
from __future__ import annotations

from .config import RiskConfig


def position_size(total_equity: float, cash: float, price: float, risk: RiskConfig,
                  commission: float = 0.0) -> int:
    """종목당 최대 비중과 보유 현금 한도 안에서 살 수 있는 주식 수."""
    if price <= 0:
        return 0
    budget = min(total_equity * risk.max_position_pct, cash)
    return max(int(budget // (price * (1 + commission))), 0)


def exit_reason(avg_price: float, current_price: float, risk: RiskConfig) -> str | None:
    """손절/익절 조건에 해당하면 사유를 반환."""
    if avg_price <= 0:
        return None
    change = current_price / avg_price - 1
    if risk.stop_loss_pct and change <= -risk.stop_loss_pct:
        return f"손절 ({change:.2%})"
    if risk.take_profit_pct and change >= risk.take_profit_pct:
        return f"익절 ({change:.2%})"
    return None
