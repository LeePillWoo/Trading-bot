"""텔레그램 알림/명령.

- 봇 토큰은 텔레그램 @BotFather 에서 발급, chat_id 는 `python -m trading_bot telegram-setup` 으로 확인.
- 설정된 chat_id 에서 온 명령만 처리한다 (다른 사람이 봇을 찾아도 조작 불가).
"""
from __future__ import annotations

import logging
import time

import requests

from .fmt import strip_tags
from .redact import redact

log = logging.getLogger(__name__)


class Notifier:
    """알림을 보내지 않는 기본 구현 (텔레그램 미설정 시)."""

    enabled = False
    # 같은 봇 토큰으로 다른 곳에서도 명령을 받고 있으면 True (텔레그램 409 Conflict)
    conflict = False

    def send(self, text: str, html: bool = False) -> None:
        log.info("[알림] %s", strip_tags(text) if html else text)

    def poll_commands(self, timeout: float = 0) -> list[str]:
        if timeout > 0:
            time.sleep(timeout)
        return []


class TelegramNotifier(Notifier):
    enabled = True

    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.session = session or requests.Session()
        self._offset: int | None = None

    def send(self, text: str, html: bool = False) -> None:
        """html=True 면 텔레그램 HTML 서식(굵게 등)으로 보낸다. 서식 오류로 거부되면 일반 텍스트로 재전송."""
        text = redact(text)
        log.info("[알림] %s", strip_tags(text) if html else text)
        payload = {"chat_id": self.chat_id, "text": text[:4000]}
        if html:
            payload["parse_mode"] = "HTML"
        try:
            resp = self.session.post(f"{self.base}/sendMessage", json=payload, timeout=10)
            if html and getattr(resp, "ok", True) is False:
                log.warning("텔레그램 서식 메시지 거부 (HTTP %s) — 일반 텍스트로 재전송",
                            getattr(resp, "status_code", "?"))
                self.session.post(f"{self.base}/sendMessage",
                                  json={"chat_id": self.chat_id, "text": strip_tags(text)[:4000]}, timeout=10)
        except requests.RequestException as e:
            # 예외 문자열에는 봇 토큰이 든 URL 이 들어가므로 종류만 남긴다
            log.warning("텔레그램 전송 실패 (%s)", type(e).__name__)

    def get_updates(self, timeout: float = 0) -> list[dict]:
        params = {"timeout": int(timeout)}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            resp = self.session.get(f"{self.base}/getUpdates", params=params, timeout=timeout + 10)
            if getattr(resp, "status_code", 200) == 409:
                # 다른 기기에서 같은 봇이 동시에 실행 중 (텔레그램이 동시 수신을 거부)
                self.conflict = True
                time.sleep(min(timeout, 5))
                return []
            updates = resp.json().get("result", [])
            self.conflict = False
        except (requests.RequestException, ValueError) as e:
            log.warning("텔레그램 수신 실패 (%s)", type(e).__name__)
            time.sleep(min(timeout, 5))
            return []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def poll_commands(self, timeout: float = 0) -> list[str]:
        """timeout 초 동안 기다리며(롱폴링) 허용된 채팅의 명령만 반환."""
        commands = []
        for u in self.get_updates(timeout):
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                commands.append(text.split()[0].split("@")[0].lower())
        return commands
