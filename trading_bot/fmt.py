"""텔레그램 메시지 서식 (HTML parse_mode).

텔레그램 봇 메시지는 글자 색을 지원하지 않는다. 그래서 굵은 글씨, 🔺(상승)/🔻(하락) 이모지,
구역 나누기로 가독성을 높인다. 종목명 등 외부에서 온 문자열은 반드시 esc() 로 감싼다
("S&T모티브" 처럼 & 가 든 이름이 있다).
"""
from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^<>]+>")


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def b(x) -> str:
    """굵게."""
    return f"<b>{esc(x)}</b>"


def i(x) -> str:
    """기울임 (부가 설명용)."""
    return f"<i>{esc(x)}</i>"


def won(v: float) -> str:
    return f"{v:,.0f}원"


def arrow(v: float) -> str:
    """한국식: 빨강(🔺)=상승, 파랑(🔻)=하락."""
    return "🔺" if v > 0 else ("🔻" if v < 0 else "▫️")


def signed_won(v: float) -> str:
    return f"{arrow(v)} {v:+,.0f}원"


def pct(v: float, digits: int = 2) -> str:
    return f"{v:+.{digits}%}"


def kdate(dt) -> str:
    """10/21(화)"""
    return f"{dt.month}/{dt.day}({'월화수목금토일'[dt.weekday()]})"


def strip_tags(text: str) -> str:
    """로그·일반 텍스트 재전송용: 태그 제거."""
    return html.unescape(_TAG.sub("", text))
