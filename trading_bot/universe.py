"""후보 종목 자동 선정: 나무증권 종목마스터(m_new_stock.mst)의 시가총액으로 코스피 상위 N개.

파일 형식 (공개, 인증 불필요): https://www.nhplug.com/instruments/m_new_stock.mst
- 레코드 237바이트 고정길이, CP949, 헤더 없음, 레코드 끝 LF
- 구조체 정의: nhplug-sdk instruments/headers/m_new_stock.h

제외: 코스닥, 우선주(코드 끝자리 0 아님), ETF·리츠·투자회사 등 일반 기업이 아닌 것,
      관리종목, 거래정지, 투자유의, 정리매매, 투자경고/위험.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

RECORD = 237
CACHE_TTL = 12 * 3600  # 하루 두 번 이상은 다시 받지 않음

# (시작, 길이) — m_new_stock.h 기준
FIELDS = {
    "code": (0, 6),
    "market": (6, 1),       # 1:코스피 4:코스닥
    "name": (7, 41),        # 선두 * (KOSPI200) / # (코스닥150) 마커
    "kind": (165, 1),       # 코스피 0:일반 4:외국기업 5:투자회사 6:리츠 8:ETF ...
    "managed": (160, 1),    # 관리종목 Y
    "halted": (161, 1),     # 거래정지 Y
    "warn": (162, 1),       # 투자유의 Y
    "cap": (174, 12),       # 전일 시가총액(억)
    "alert": (188, 1),      # 0:해당없음, 그 외 투자주의/경고/위험
    "liquidation": (189, 1),  # 정리매매 Y
}


@dataclass
class Listing:
    code: str
    name: str
    market: str
    kind: str
    cap: float  # 억원
    managed: bool
    halted: bool
    warn: bool
    alert: str
    liquidation: bool

    @property
    def is_common_kospi(self) -> bool:
        return self.market == "1" and self.kind == "0" and self.code.endswith("0")

    @property
    def is_risky(self) -> bool:
        return (self.managed or self.halted or self.warn or self.liquidation
                or self.alert not in ("", "0"))


def parse_master(raw: bytes) -> list[Listing]:
    out = []
    for i in range(0, len(raw) - RECORD + 1, RECORD):
        rec = raw[i:i + RECORD]
        f = {k: rec[s:s + n].decode("cp949", errors="replace").strip() for k, (s, n) in FIELDS.items()}
        try:
            cap = float(f["cap"].replace(",", "") or 0)
        except ValueError:
            cap = 0.0
        out.append(Listing(
            code=f["code"], name=f["name"].lstrip("*#").strip(), market=f["market"], kind=f["kind"],
            cap=cap, managed=f["managed"] == "Y", halted=f["halted"] == "Y", warn=f["warn"] == "Y",
            alert=f["alert"], liquidation=f["liquidation"] == "Y",
        ))
    return out


def top_kospi(listings: list[Listing], n: int) -> list[Listing]:
    ok = [x for x in listings if x.is_common_kospi and not x.is_risky and x.cap > 0]
    return sorted(ok, key=lambda x: x.cap, reverse=True)[:n]


def master_url(domain: str | None = None) -> str:
    domain = domain or os.environ.get("NHPLUG_DOMAIN", "nhplug.com")
    return f"https://www.{domain}/instruments/m_new_stock.mst"


def load_master(cache_dir: str | Path = ".cache", session: requests.Session | None = None,
                domain: str | None = None) -> list[Listing]:
    """종목마스터를 받아(12시간 캐시) 파싱한다."""
    path = Path(cache_dir) / "m_new_stock.mst"
    if not path.exists() or time.time() - path.stat().st_mtime > CACHE_TTL:
        try:
            resp = (session or requests).get(master_url(domain), timeout=30)
            resp.raise_for_status()
            if len(resp.content) % RECORD:
                raise ValueError(f"종목마스터 크기 이상 ({len(resp.content)}바이트)")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        except Exception as e:
            if not path.exists():
                raise RuntimeError(f"종목마스터 다운로드 실패: {type(e).__name__}") from None
            log.warning("종목마스터 갱신 실패, 이전 파일 사용 (%s)", type(e).__name__)
    return parse_master(path.read_bytes())


def kospi_top_symbols(n: int, **kw) -> tuple[list[str], dict[str, str]]:
    """(코스피 시총 상위 n 종목코드, 코드→종목명)."""
    top = top_kospi(load_master(**kw), n)
    return [x.code for x in top], {x.code: x.name for x in top}
