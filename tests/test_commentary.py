from trading_bot.commentary import close_comment, morning_comment, short_reason
from trading_bot.fmt import strip_tags

label = lambda s: {"A": "에이", "B": "비"}.get(s, s)  # noqa: E731


def test_morning_no_trades():
    t = strip_tags(morning_comment([], ["x", "y"], 3, label, -0.036, "rsi"))
    assert t.startswith("💬 총평 어제 시장(코스피200) -3.6%.")
    assert "보유 3종목 모두 아직 매도 신호(RSI 70 위)가 안 나와 그대로 들고 갑니다" in t
    assert "매수 신호가 2종목 더 있었지만 현금이 모자라 못 샀습니다" in t


def test_morning_with_trades():
    trades = [{"side": "sell", "symbol": "A", "qty": 2, "buy": 100, "sell": 90,
               "reason": "매도 신호: 상황 판단 손절: 더 빠지는데 외국인+기관이 계속 팔고 있음 (큰손 이탈)"},
              {"side": "buy", "symbol": "B", "qty": 1, "buy": 50, "reason": "11/4 RSI 28로 과매도(30 아래) → 반등 노림"}]
    t = strip_tags(morning_comment(trades, [], 2, label, None, "rsi"))
    assert "에이: 큰손 이탈로 손절 (-20원)." in t
    assert "비: 과매도(RSI 30 아래) 반등 노림 → 매수." in t
    assert "나머지 보유 1종목은" in t
    assert short_reason("과매수: 어제 RSI 74 (70 위) → 차익 실현") == "과매수(RSI 70 위) 차익 실현"


def test_close_comment_sell_tomorrow_and_market_compare():
    h = [("에이", 0.03, {"rsi": 41, "sell_tomorrow": False, "room": 0.05}),
         ("비", -0.02, {"rsi": 72, "sell_tomorrow": True, "why": "과매수 RSI 72, 차익 실현"})]
    t = strip_tags(close_comment(700, 100_000, 0.02, h, 30_000, 20_000))
    assert "오늘 봇 손익 +700원 (+0.7%)" in t and "시장보다 부진" in t
    assert "에이 +3.0%로 가장 좋았고, 비 -2.0%로 가장 약했습니다" in t
    assert "내일 아침 매도 예정: 비(과매수 RSI 72, 차익 실현)" in t
    assert "새 매수는 제한적" not in t  # 현금 3만 ≥ 몫 2만
    t2 = strip_tags(close_comment(-500, 100_000, -0.03, h[:1], 10_000, 20_000, live=True))
    assert "시장보다 선방" in t2 and "지금 가격 기준으로는 내일 아침 매도 신호 없음" in t2
    assert "손절선까지 가장 가까운 종목은 에이 (여유 5%p)" in t2 and "새 매수는 제한적" in t2
