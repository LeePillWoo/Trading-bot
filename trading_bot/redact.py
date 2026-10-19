"""로그·알림에서 비밀값(앱키, 시크릿, 토큰)을 가린다.

requests 의 네트워크 오류 메시지에는 요청 URL 이 그대로 들어가므로
(예: 텔레그램 URL 의 봇 토큰, 나무증권 토큰 발급 URL 의 앱키/시크릿)
파일 로그와 텔레그램으로 나가기 전에 한 번 더 걸러낸다.
"""
from __future__ import annotations

import logging
import os
import re

SECRET_ENV_KEYS = ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "TELEGRAM_BOT_TOKEN",
                   "KIS_APP_KEY", "KIS_APP_SECRET")
# 값을 몰라도 형태로 잡는 패턴: 텔레그램 봇 토큰, URL 쿼리의 키/시크릿
PATTERNS = [
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)((?:appkey|appsecretkey|appsecret|secret|token)=)[^&\s'\")]+"),
]
MASK = "***"


def secret_values() -> list[str]:
    return [v for k in SECRET_ENV_KEYS if len(v := os.environ.get(k, "").strip()) >= 6]


def redact(text: str) -> str:
    text = str(text)
    for value in secret_values():
        text = text.replace(value, MASK)
    text = PATTERNS[0].sub(MASK, text)
    return PATTERNS[1].sub(lambda m: m.group(1) + MASK, text)


class RedactingFormatter(logging.Formatter):
    """메시지와 예외 traceback 을 포함한 최종 로그 줄 전체를 가린다."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
