"""한국투자증권(KIS) Open API REST 브로커 (국내주식).

API 문서: https://apiportal.koreainvestment.com
- 접근토큰은 하루 단위로 발급되며 발급 요청 빈도 제한이 있어 파일에 캐시한다.
- 모의투자 서버는 초당 요청 수 제한이 낮으므로 요청 간 간격을 둔다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ..config import KisConfig
from .base import Account, Broker, OrderResult, Position

log = logging.getLogger(__name__)

REAL_URL = "https://openapi.koreainvestment.com:9443"
PAPER_URL = "https://openapivts.koreainvestment.com:29443"

# 거래ID (실전, 모의)
TR_IDS = {
    "daily_chart": ("FHKST03010100", "FHKST03010100"),
    "price": ("FHKST01010100", "FHKST01010100"),
    "buy": ("TTTC0012U", "VTTC0012U"),
    "sell": ("TTTC0011U", "VTTC0011U"),
    "balance": ("TTTC8434R", "VTTC8434R"),
}


class KisApiError(RuntimeError):
    pass


class KisBroker(Broker):
    def __init__(self, config: KisConfig, token_cache: str | Path = ".kis_token.json",
                 session: requests.Session | None = None):
        self.cfg = config
        self.base_url = REAL_URL if config.is_real else PAPER_URL
        self.token_cache = Path(token_cache)
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expiry = datetime.min
        self._last_call = 0.0
        self._min_interval = 0.06 if config.is_real else 0.55

    # ---------- 인증 ----------
    def _access_token(self) -> str:
        now = datetime.now()
        if self._token and now < self._token_expiry:
            return self._token
        if self.token_cache.exists():
            try:
                cached = json.loads(self.token_cache.read_text())
                expiry = datetime.fromisoformat(cached["expiry"])
                if cached.get("mode") == self.cfg.mode and cached.get("app_key") == self.cfg.app_key \
                        and now < expiry:
                    self._token, self._token_expiry = cached["token"], expiry
                    return self._token
            except (ValueError, KeyError):
                pass
        resp = self.session.post(f"{self.base_url}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }, timeout=10)
        data = resp.json()
        if "access_token" not in data:
            raise KisApiError(f"토큰 발급 실패: {data}")
        self._token = data["access_token"]
        # 만료 10분 전에 갱신
        self._token_expiry = now + timedelta(seconds=int(data.get("expires_in", 86400)) - 600)
        self.token_cache.write_text(json.dumps({
            "token": self._token, "expiry": self._token_expiry.isoformat(),
            "mode": self.cfg.mode, "app_key": self.cfg.app_key,
        }))
        try:
            self.token_cache.chmod(0o600)
        except OSError:
            pass
        return self._token

    def _headers(self, tr_key: str) -> dict:
        tr_id = TR_IDS[tr_key][0 if self.cfg.is_real else 1]
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token()}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _request(self, method: str, path: str, tr_key: str, *, params=None, body=None) -> dict:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = self.session.request(method, f"{self.base_url}{path}", headers=self._headers(tr_key),
                                    params=params, json=body, timeout=10)
        self._last_call = time.monotonic()
        try:
            data = resp.json()
        except ValueError:
            raise KisApiError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
        if data.get("rt_cd") != "0":
            raise KisApiError(f"[{data.get('msg_cd')}] {data.get('msg1')}")
        return data

    # ---------- 시세 ----------
    def daily_ohlcv(self, symbol: str, days: int = 120) -> pd.DataFrame:
        rows: list[dict] = []
        end = datetime.now()
        # 한 번에 최대 100건이므로 기간을 나눠 조회
        while len(rows) < days:
            start = end - timedelta(days=140)
            data = self._request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                 "daily_chart", params={
                                     "FID_COND_MRKT_DIV_CODE": "J",
                                     "FID_INPUT_ISCD": symbol,
                                     "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                                     "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                                     "FID_PERIOD_DIV_CODE": "D",
                                     "FID_ORG_ADJ_PRC": "0",  # 수정주가
                                 })
            chunk = [r for r in data.get("output2", []) if r.get("stck_bsop_date")]
            if not chunk:
                break
            rows.extend(chunk)
            oldest = min(r["stck_bsop_date"] for r in chunk)
            end = datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)
        if not rows:
            raise KisApiError(f"{symbol} 일봉 데이터가 없습니다")
        df = pd.DataFrame({
            "date": pd.to_datetime([r["stck_bsop_date"] for r in rows]),
            "open": [float(r["stck_oprc"]) for r in rows],
            "high": [float(r["stck_hgpr"]) for r in rows],
            "low": [float(r["stck_lwpr"]) for r in rows],
            "close": [float(r["stck_clpr"]) for r in rows],
            "volume": [float(r["acml_vol"]) for r in rows],
        })
        df = df.drop_duplicates("date").set_index("date").sort_index()
        return df.tail(days)

    def current_price(self, symbol: str) -> float:
        data = self._request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "price",
                             params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        return float(data["output"]["stck_prpr"])

    # ---------- 계좌 ----------
    def account(self) -> Account:
        data = self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", "balance", params={
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_product,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        })
        positions = {}
        for r in data.get("output1", []):
            qty = int(r.get("hldg_qty", 0))
            if qty > 0:
                positions[r["pdno"]] = Position(r["pdno"], qty, float(r["pchs_avg_pric"]), float(r["prpr"]))
        summary = (data.get("output2") or [{}])[0]
        # D+2 예수금(정산 예정 반영)을 주문 가능 현금으로 사용
        cash = float(summary.get("prvs_rcdl_excc_amt") or summary.get("dnca_tot_amt") or 0)
        return Account(cash=cash, positions=positions)

    # ---------- 주문 ----------
    def _order(self, side: str, symbol: str, qty: int, price: float) -> OrderResult:
        body = {
            "CANO": self.cfg.account_no,
            "ACNT_PRDT_CD": self.cfg.account_product,
            "PDNO": symbol,
            "ORD_DVSN": "00" if price else "01",  # 00 지정가, 01 시장가
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)) if price else "0",
        }
        try:
            data = self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash", side, body=body)
        except KisApiError as e:
            return OrderResult(symbol, side, qty, price, ok=False, message=str(e))
        return OrderResult(symbol, side, qty, price, order_id=data.get("output", {}).get("ODNO", ""),
                           message=data.get("msg1", ""))

    def buy(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        return self._order("buy", symbol, qty, price)

    def sell(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        return self._order("sell", symbol, qty, price)
