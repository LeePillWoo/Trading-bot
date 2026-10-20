"""기기 이사용: 봇 상태(장부·재진입 금지·월간 리밸런싱 기록)를 한 줄 텍스트로 내보내고 가져온다.

상태 파일에는 키·토큰이 없다 (그건 .env 에만 있음). 그래서 텔레그램으로 주고받아도 된다.
새 기기에서 이 상태를 가져오지 않으면, 이전 기기의 봇이 산 종목을 새 봇이 모르고 팔지 못한다.
"""
from __future__ import annotations

import json
from pathlib import Path

PREFIX = "TBSTATE:"


def export_text(paths: list[Path]) -> str:
    files = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in paths if p.exists()}
    return PREFIX + json.dumps({"files": files}, ensure_ascii=False, separators=(",", ":"))


def import_text(text: str, directory: str | Path = ".") -> list[str]:
    """내보낸 한 줄을 받아 상태 파일을 쓴다 (기존 파일은 .bak 으로 보관). 쓴 파일 이름 목록 반환."""
    text = text.strip()
    if not text.startswith(PREFIX):
        raise ValueError(f"'{PREFIX}' 로 시작하는 내보내기 텍스트가 아닙니다")
    files = json.loads(text[len(PREFIX):])["files"]
    written = []
    for name, data in files.items():
        if not (name.startswith("bot_state_") and name.endswith(".json") and "/" not in name and "\\" not in name):
            raise ValueError(f"상태 파일 이름이 아닙니다: {name}")
        path = Path(directory) / name
        if path.exists():
            path.replace(path.with_suffix(".json.bak"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(name)
    return written
