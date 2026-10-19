"""설정 로딩: 환경변수(.env)와 기본 전략/리스크 파라미터."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """간단한 .env 로더 (외부 의존성 없이). 이미 설정된 환경변수는 덮어쓰지 않는다."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class KisConfig:
    app_key: str
    app_secret: str
    account_no: str  # 계좌번호 앞 8자리
    account_product: str  # 상품코드 2자리
    mode: str = "paper"  # paper(모의투자) | real(실전투자)

    @property
    def is_real(self) -> bool:
        return self.mode == "real"

    @classmethod
    def from_env(cls) -> "KisConfig":
        account = os.environ.get("KIS_ACCOUNT", "")
        if "-" in account:
            no, product = account.split("-", 1)
        else:
            no, product = account[:8], account[8:] or "01"
        mode = os.environ.get("KIS_MODE", "paper").lower()
        if mode not in ("paper", "real"):
            raise ValueError(f"KIS_MODE는 paper 또는 real 이어야 합니다: {mode}")
        cfg = cls(
            app_key=os.environ.get("KIS_APP_KEY", ""),
            app_secret=os.environ.get("KIS_APP_SECRET", ""),
            account_no=no,
            account_product=product,
            mode=mode,
        )
        missing = [n for n, v in (("KIS_APP_KEY", cfg.app_key), ("KIS_APP_SECRET", cfg.app_secret),
                                  ("KIS_ACCOUNT", cfg.account_no)) if not v]
        if missing:
            raise ValueError(f"환경변수가 설정되지 않았습니다: {', '.join(missing)}")
        return cfg


@dataclass
class NamuhConfig:
    """나무증권(NH투자증권) Open API 설정."""

    app_key: str
    app_secret: str
    account_no: str  # /n2/acctinfo 의 acct_no (운영 01·02, 모의투자 03 계좌)
    mode: str = "paper"  # paper(모의투자 moapi) | real(운영 api)
    domain: str = "nhplug.com"  # 나무 = nhplug.com, N2 = n2plug.com

    @property
    def is_real(self) -> bool:
        return self.mode == "real"

    @classmethod
    def from_env(cls) -> "NamuhConfig":
        mode = os.environ.get("NHPLUG_MODE", "paper").lower()
        if mode not in ("paper", "real"):
            raise ValueError(f"NHPLUG_MODE는 paper 또는 real 이어야 합니다: {mode}")
        domain = os.environ.get("NHPLUG_DOMAIN", "nhplug.com").lower()
        if domain not in ("nhplug.com", "n2plug.com"):
            raise ValueError(f"NHPLUG_DOMAIN은 nhplug.com 또는 n2plug.com 이어야 합니다: {domain}")
        cfg = cls(
            app_key=os.environ.get("NHPLUG_APP_KEY", ""),
            app_secret=os.environ.get("NHPLUG_APP_SECRET", ""),
            account_no=os.environ.get("NHPLUG_ACCOUNT", "").replace("-", ""),
            mode=mode,
            domain=domain,
        )
        missing = [n for n, v in (("NHPLUG_APP_KEY", cfg.app_key), ("NHPLUG_APP_SECRET", cfg.app_secret))
                   if not v]
        if missing:
            raise ValueError(f"환경변수가 설정되지 않았습니다: {', '.join(missing)}")
        return cfg


@dataclass
class RiskConfig:
    # 종목당 최대 투자 비중 (총 자산 대비)
    max_position_pct: float = 0.2
    # 손절 비율 (매수 평균가 대비 하락률)
    stop_loss_pct: float = 0.07
    # 익절 비율 (0 이면 사용 안 함)
    take_profit_pct: float = 0.0
    # 하루 최대 주문 횟수 (폭주 방지, 도달하면 일시정지)
    max_orders_per_day: int = 20
    # 하루 손실 한도 (장 시작 시 총자산 대비, 도달하면 일시정지. 0 이면 사용 안 함)
    max_daily_loss_pct: float = 0.03


@dataclass
class BotConfig:
    symbols: list[str] = field(default_factory=lambda: ["005930"])  # 삼성전자
    strategy: str = "sma"
    strategy_params: dict = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    # 장중 점검 주기(초)
    interval_sec: int = 300
    # 매매 비용
    commission: float = 0.00015  # 매수/매도 수수료 0.015%
    sell_tax: float = 0.002  # 매도 시 증권거래세 (시장/연도에 따라 다르므로 확인 필요)
