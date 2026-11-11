"""보고서 끝에 붙는 💬 총평 (규칙 기반 문장 조합, 외부 AI 호출 없음).

- 아침(매매) 보고: 오늘 왜 사고팔았는지 / 왜 안 했는지 한두 문장
- 마감 보고·/report: 오늘 봇 손익과 시장 비교, 보유 종목 중 제일 좋았던/약했던 것, 내일 아침 예정 행동
"""
from __future__ import annotations

from .fmt import esc, i, won


def short_reason(reason: str) -> str:
    """매매 이유 문장을 총평용 짧은 표현으로."""
    r = reason or ""
    if "손절" in r:
        if "큰손" in r:
            return "큰손 이탈로 손절"
        if "시장보다" in r:
            return "시장 대비 급락으로 손절"
        if "안전장치" in r:
            return "최후 안전장치 손절"
        return "상황 판단 손절"
    if "과매수" in r:
        return "과매수(RSI 70 위) 차익 실현"
    if "과매도" in r:
        return "과매도(RSI 30 아래) 반등 노림"
    if "후보 종목에서 제외" in r:
        return "후보 제외로 정리"
    if "리밸런싱" in r:
        return "순위 교체"
    if "익절" in r:
        return "익절"
    return ""


def morning_comment(trades: list[dict], unaffordable: list[str], n_hold: int, label, market_chg: float | None,
                    strategy_name: str = "", halted: bool = False) -> str:
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]
    parts = []
    if market_chg is not None:
        parts.append(f"어제 시장(코스피200) {market_chg:+.1%}.")
    if halted:
        parts.append("거래 중단으로 일부 주문이 나가지 못했습니다.")
    hold_why = "아직 매도 신호(RSI 70 위)가 안 나와" if strategy_name == "rsi" else "매도 신호가 없어"
    if not sells and not buys:
        if n_hold:
            parts.append(f"오늘은 매매 없음. 보유 {n_hold}종목 모두 {hold_why} 그대로 들고 갑니다.")
        elif unaffordable:
            parts.append("오늘은 매매 없음.")
        else:
            parts.append("오늘은 매매 없음. 보유 종목도, 매수 신호도 없습니다.")
    else:
        for t in sells:
            pnl = (t["sell"] - t["buy"]) * t["qty"]
            why = short_reason(t.get("reason", "")) or "매도 신호"
            parts.append(f"{label(t['symbol'])}: {why} ({pnl:+,.0f}원).")
        if buys:
            why = short_reason(buys[0].get("reason", "")) or "매수 신호"
            parts.append(f"{', '.join(label(t['symbol']) for t in buys)}: {why} → 매수.")
        rest = n_hold - len(buys)
        if rest > 0:
            parts.append(f"나머지 보유 {rest}종목은 {hold_why} 보유.")
    if unaffordable:
        parts.append(f"매수 신호가 {len(unaffordable)}종목 더 있었지만 현금이 모자라 못 샀습니다.")
    return f"💬 {i('총평')} " + esc(" ".join(parts))


def close_comment(today_pnl: float, base: float, market_chg: float | None,
                  holdings: list[tuple[str, float | None, dict]], cash: float, slot: float,
                  realized_today: float = 0.0, live: bool = False) -> str:
    """holdings: (종목명, 오늘 등락률 또는 None, 전략 outlook dict). live=장중(현재가 기준 추정)."""
    parts = []
    p = f"오늘 봇 손익 {today_pnl:+,.0f}원" + (f" ({today_pnl / base:+.1%})" if base else "")
    if realized_today:
        p += f", 그중 실현 {realized_today:+,.0f}원"
    if market_chg is not None:
        p += f". 시장(코스피200) {market_chg:+.1%}"
        if base:
            diff = today_pnl / base - market_chg
            if diff >= 0.005:
                p += " → 시장보다 선방"
            elif diff <= -0.005:
                p += " → 시장보다 부진"
    parts.append(p + ".")
    moved = [h for h in holdings if h[1] is not None]
    if len(moved) >= 2:
        best, worst = max(moved, key=lambda h: h[1]), min(moved, key=lambda h: h[1])
        parts.append(f"{best[0]} {best[1]:+.1%}로 가장 좋았고, {worst[0]} {worst[1]:+.1%}로 가장 약했습니다.")
    elif len(moved) == 1:
        parts.append(f"{moved[0][0]} 오늘 {moved[0][1]:+.1%}.")
    if holdings:
        selling = [(n, o) for n, _, o in holdings if o.get("sell_tomorrow")]
        head = "지금 가격 기준으로는 " if live else ""
        if selling:
            parts.append(head + "내일 아침 매도 예정: "
                         + ", ".join(f"{n}({o.get('why', '매도 신호')})" for n, o in selling) + ".")
        else:
            near = [(n, o["room"]) for n, _, o in holdings if o.get("room") is not None]
            rsis = [(n, o["rsi"]) for n, _, o in holdings if o.get("rsi") is not None]
            s = head + "내일 아침 매도 신호 없음, 전부 보유 유지"
            if rsis:
                hi = max(rsis, key=lambda x: x[1])
                s += f" (RSI 최고 {hi[0]} {hi[1]:.0f}, 70 넘으면 매도)"
            if near:
                lo = min(near, key=lambda x: x[1])
                s += f". 손절선까지 가장 가까운 종목은 {lo[0]} (여유 {lo[1]:.0%}p)" if lo[1] < 0.08 else ""
            parts.append(s + ".")
    else:
        parts.append("보유 종목 없음. 매수 신호가 나오면 아침에 삽니다.")
    if slot and cash < slot:
        parts.append(f"현금 {won(cash)}이라 새 매수는 제한적입니다 (한 종목 몫 {won(slot)}).")
    return f"💬 {i('총평')} " + esc(" ".join(parts))
