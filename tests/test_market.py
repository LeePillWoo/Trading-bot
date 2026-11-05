import pandas as pd

from trading_bot.broker.paper import PaperBroker
from trading_bot.market import market_report
from trading_bot.strategy import make_strategy


def series(slope, n=90):
    idx = pd.bdate_range("2026-07-01", periods=n)
    c = pd.Series([100.0 + slope * k for k in range(n)], index=idx)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def test_market_report_explains_filter():
    prices = {"069500": series(0.5), "A": series(1.0), "B": series(-0.3), "C": series(-0.5)}
    broker = PaperBroker(prices=prices)
    strat = make_strategy("rotation", top_n=2, market_ma=60)
    text = market_report(broker, ["A", "B", "C"], strat, {"A": "에이"})
    assert "[시장] KODEX 200" in text
    assert "60일선 위 1/3종목 (필요 2) → 미통과 (전부 현금)" in text
    assert "지금 판단하면 살 종목: 없음" in text
    assert text.index("에이") < text.index(" B")  # 모멘텀 순 정렬

    prices["B"] = series(0.2)
    text = market_report(PaperBroker(prices=prices), ["A", "B", "C"], strat, {})
    assert "통과 (매수 가능)" in text and "살 종목: A(A), B(B)" in text


def test_market_report_filter_off():
    prices = {"069500": series(0.5), "A": series(1.0), "B": series(-0.3), "C": series(-0.5)}
    text = market_report(PaperBroker(prices=prices), ["A", "B", "C"], make_strategy("rotation", top_n=5), {})
    assert "시장 필터: 꺼짐" in text and "살 종목: A(A)" in text


def test_market_report_daily_strategy_reasons():
    prices = {"069500": series(0.5), "A": series(1.0)}
    text = market_report(PaperBroker(prices=prices), ["A"], make_strategy("rsi"), {"A": "에이"})
    assert "[rsi 판단]" in text and "에이(A)" in text and "RSI" in text
