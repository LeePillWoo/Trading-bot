"""대화형 설정 마법사: 키를 물어보고 .env 를 만들고, 텔레그램 chat_id 와 계좌번호를 자동으로 찾는다.

사용: python -m trading_bot setup   (다시 실행하면 Enter 로 기존 값을 유지)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .notify import TelegramNotifier

DEFAULT_BOT_ARGS = "--symbols 005930 000660 --strategy sma --max-position 0.2 --stop-loss 0.07 --max-daily-loss 0.03"
ACCOUNT_TYPES = {"01": "실전(일반)", "02": "실전(주문대리인)", "03": "모의투자"}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def save_env(path: Path, updates: dict[str, str]) -> None:
    """기존 줄(주석 포함)은 유지하고 해당 키만 바꾸거나 맨 끝에 추가한다."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def find_chat_id(tg: TelegramNotifier, wait: float = 120) -> str | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for u in tg.get_updates(timeout=min(20, max(1, deadline - time.monotonic()))):
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
    return None


class Wizard:
    def __init__(self, env_path: str | Path = ".env", ask: Callable[[str], str] = input,
                 telegram_factory=TelegramNotifier, broker_factory=None):
        self.path = Path(env_path)
        self.ask_fn = ask
        self.telegram_factory = telegram_factory
        self.broker_factory = broker_factory
        self.env = read_env(self.path)

    def ask(self, prompt: str, key: str | None = None, default: str = "", secret: bool = False) -> str:
        current = self.env.get(key, "") if key else ""
        if current:
            hint = "설정됨, Enter=유지" if secret else f"{current}, Enter=유지"
        elif default:
            hint = f"{default}, Enter=기본값"
        else:
            hint = ""
        answer = self.ask_fn(f"{prompt}{f' [{hint}]' if hint else ''}: ").strip()
        return answer or current or default

    def set(self, updates: dict[str, str]) -> None:
        self.env.update(updates)
        os.environ.update(updates)
        save_env(self.path, updates)

    def run(self) -> int:
        print("\n=== 자동매매 봇 설정 ===  (붙여넣기 후 Enter)\n")

        # 1) 나무증권
        print("[1/4] 나무증권 Open API (nhplug.com 에서 발급받은 값)")
        key = self.ask("  앱키", "NHPLUG_APP_KEY", secret=True)
        secret = self.ask("  시크릿", "NHPLUG_APP_SECRET", secret=True)
        if not key or not secret:
            print("앱키와 시크릿이 필요합니다. 다시 실행하세요.")
            return 1
        current_mode = self.env.get("NHPLUG_MODE", "paper")
        choice = self.ask("  모드: 1=모의투자, 2=실전", default="1" if current_mode != "real" else "2")
        mode = "real" if choice == "2" else "paper"
        if mode == "real" and self.ask_fn("  ⚠️ 실전은 실제 돈으로 주문합니다. 계속하려면 '실전' 입력: ").strip() != "실전":
            print("  모의투자로 진행합니다.")
            mode = "paper"
        self.set({"NHPLUG_APP_KEY": key, "NHPLUG_APP_SECRET": secret, "NHPLUG_MODE": mode,
                  "NHPLUG_DOMAIN": self.env.get("NHPLUG_DOMAIN", "nhplug.com")})

        # 2) 계좌
        print("\n[2/4] 계좌 확인 중...")
        try:
            broker = self._broker()
            accounts = broker.list_accounts()
        except Exception as e:
            print(f"  ❌ 나무증권 연결 실패: {e}\n  앱키/시크릿을 확인하고 다시 실행하세요.")
            return 1
        want = ("01", "02") if mode == "real" else ("03",)
        usable = [a for a in accounts if str(a.get("acct_type", "")).strip() in want]
        for a in accounts:
            t = str(a.get("acct_type", "")).strip()
            print(f"  {a.get('acct_no')}  {ACCOUNT_TYPES.get(t, t)}{'  ✅' if a in usable else ''}")
        if not usable:
            kind = "실전" if mode == "real" else "모의투자"
            print(f"  ❌ {kind} 계좌가 없습니다. nhplug.com 에서 {kind} 신청 여부를 확인하세요.")
            return 1
        account = str(usable[0]["acct_no"])
        if len(usable) > 1:
            for i, a in enumerate(usable, 1):
                print(f"  {i}) {a['acct_no']}")
            pick = self.ask("  사용할 계좌 번호", default="1")
            account = str(usable[int(pick) - 1]["acct_no"]) if pick.isdigit() and 0 < int(pick) <= len(usable) else account
        self.set({"NHPLUG_ACCOUNT": account})
        print(f"  → 계좌 {account} 사용")

        # 3) 텔레그램
        print("\n[3/4] 텔레그램 (@BotFather 에서 만든 봇 토큰)")
        token = self.ask("  봇 토큰", "TELEGRAM_BOT_TOKEN", secret=True)
        if token:
            self.set({"TELEGRAM_BOT_TOKEN": token})
            tg = self.telegram_factory(token, self.env.get("TELEGRAM_CHAT_ID", ""))
            if not self.env.get("TELEGRAM_CHAT_ID") or self.ask("  chat_id 를 다시 찾을까요? y/N", default="n").lower() == "y":
                print("  👉 지금 텔레그램에서 내 봇에게 아무 메시지나 보내세요. (2분 대기)")
                chat_id = find_chat_id(tg)
                if not chat_id:
                    print("  ❌ 메시지를 받지 못했습니다. 토큰을 확인하고 다시 실행하세요.")
                    return 1
                tg.chat_id = chat_id
                self.set({"TELEGRAM_CHAT_ID": chat_id})
            tg.send("✅ 자동매매 봇 연결 완료")
            print("  → 텔레그램 연결 완료 (메시지를 확인하세요)")
        else:
            print("  → 텔레그램 없이 진행 (알림/명령 사용 불가)")

        # 4) 종목
        print("\n[4/4] 매매 설정")
        bot_args = self.env.get("BOT_ARGS") or DEFAULT_BOT_ARGS
        parts = bot_args.replace("--confirm-real", "").split()
        if "--symbols" not in parts:
            parts = ["--symbols", "005930", *parts]
        i = parts.index("--symbols")
        j = i + 1
        while j < len(parts) and not parts[j].startswith("--"):
            j += 1
        symbols = self.ask("  매매할 종목코드 (띄어쓰기로 구분)", default=" ".join(parts[i + 1:j]))
        parts[i + 1:j] = symbols.split()
        if mode == "real":
            parts.append("--confirm-real")
        self.set({"BOT_ARGS": " ".join(parts)})
        print(f"  → {' '.join(parts)}")

        print("\n✅ 설정 완료 (.env 저장됨)")
        return 0

    def _broker(self):
        if self.broker_factory:
            return self.broker_factory()
        from .broker.namuh import NamuhBroker
        from .config import NamuhConfig
        return NamuhBroker(NamuhConfig.from_env())
