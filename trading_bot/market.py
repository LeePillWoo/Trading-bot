"""시장 점검 보고서: 봇이 왜 사고/안 사는지 숫자로 보여준다.

`python -m trading_bot market` — .env 의 BOT_ARGS(종목·전략 파라미터)를 그대로 읽어서
- 시장 기준 (KODEX 200 = 코스피200 추종, 데이터 조회용) 의 최근 흐름
- 후보 종목별 최근 2주 수익률, 60일 이동평균 대비 위치, 모멘텀(lookback) 수익률 순위
- 시장 필터(후보 중 이동평균 위 종목 비율) 통과 여부와 '지금 판단하면 살 종목'
을 출력한다. 조회만 하고 주문은 내지 않는다.
"""
from __future__ import annotations

import pandas as pd

BENCHMARK = ("069500", "KODEX 200 (코스피200)")


def _row(df: pd.DataFrame, ma: int, lookback: int) -> dict | None:
    c = df["close"].dropna()
    if len(c) < 11:
        return None
    last = float(c.iloc[-1])
    return {
        "close": last,
        "date": c.index[-1],
        "d1": last / float(c.iloc[-2]) - 1,
        "w2": last / float(c.iloc[-11]) - 1,  # 최근 10거래일 ≈ 2주
        "vs_ma": last / float(c.iloc[-ma:].mean()) - 1 if len(c) >= ma else None,
        "mom": last / float(c.iloc[-1 - lookback]) - 1 if len(c) > lookback else None,
    }


def market_report(broker, symbols: list[str], strategy, names: dict[str, str]) -> str:
    ma = getattr(strategy, "market_ma", 0) or 60
    lookback = getattr(strategy, "lookback", 60)
    days = max(ma, lookback) + 15
    label = lambda s: f"{names.get(s, s)}({s})"  # noqa: E731
    lines = []

    try:
        bm = _row(broker.daily_ohlcv(BENCHMARK[0], days), ma, lookback)
    except Exception as e:  # 벤치마크는 참고용
        bm, err = None, e
    if bm:
        lines += [f"[시장] {BENCHMARK[1]} · 기준일 {bm['date']:%m/%d}",
                  f"  최근 2주 {bm['w2']:+.1%} · 전일 대비 {bm['d1']:+.1%} · {ma}일선 대비 {bm['vs_ma']:+.1%}"
                  f" ({'위' if bm['vs_ma'] > 0 else '아래'})", ""]
    else:
        lines += [f"[시장] 조회 실패: {err}", ""]

    rows, closes = [], {}
    for s in symbols:
        try:
            df = broker.daily_ohlcv(s, days)
        except Exception as e:
            lines.append(f"  {label(s)} 조회 실패: {e}")
            continue
        r = _row(df, ma, lookback)
        if r:
            rows.append((s, r))
            closes[s] = df["close"].dropna()
    rows.sort(key=lambda x: x[1]["mom"] if x[1]["mom"] is not None else -9, reverse=True)

    above = [s for s, r in rows if r["vs_ma"] is not None and r["vs_ma"] > 0]
    lines.append(f"[후보 {len(rows)}종목] 순위 = 최근 {lookback}거래일 수익률")
    lines.append(f"{'':2}{'종목':<14}{'2주':>7}{f'{ma}일선':>8}{f'{lookback}일':>8}")
    for k, (s, r) in enumerate(rows, 1):
        vs = f"{r['vs_ma']:+.1%}" if r["vs_ma"] is not None else "-"
        mom = f"{r['mom']:+.1%}" if r["mom"] is not None else "-"
        mark = "↑" if s in above else " "
        lines.append(f"{k:>2} {names.get(s, s)[:7]:<9}{mark} {r['w2']:>+7.1%}{vs:>8}{mom:>8}")

    lines.append("")
    w2 = [r["w2"] for _, r in rows]
    if w2:
        lines.append(f"후보 평균 2주 수익률 {sum(w2) / len(w2):+.1%} · 오른 종목 {sum(x > 0 for x in w2)}/{len(w2)}")
    need = (len(rows) + 1) // 2
    lines.append(f"시장 필터: {ma}일선 위 {len(above)}/{len(rows)}종목 (필요 {need}) → "
                 + ("통과 (매수 가능)" if len(above) * 2 >= len(rows) else "미통과 (전부 현금)"))
    if hasattr(strategy, "select") and closes:
        picked = strategy.select(closes)
        lines.append("지금 판단하면 살 종목: " + (", ".join(label(s) for s in picked) if picked else "없음"))
    return "\n".join(lines)
