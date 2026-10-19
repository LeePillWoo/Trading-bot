"""NH투자증권 나무증권 Open API(NHPLUG) REST 브로커 (국내주식).

명세: https://www.nhplug.com/llms.txt · 공식 SDK: https://github.com/PLUG-OpenAPI/nhplug-sdk
- 모든 호출은 POST + {"Input_0": {...}}, 응답은 Output_0/Output_1 + rsp_cd/rsp_msg.
- 접근토큰은 운영(api) 서버에서만 발급되며 24시간 유효. 재발급할 때마다 보안 알림이 가므로
  파일에 캐시하고 401(토큰 무효)일 때만 재발급한다.
- 시세 API는 운영 서버 전용(모의투자 미제공)이라, 모의투자 모드에서도 시세는 운영 서버에서 받고
  주문·잔고만 모의투자(moapi) 서버로 보낸다.
- rsp_cd 값은 API마다 의미가 달라 코드로 성공을 판정하지 않는다 (SDK 규약).
  주문은 주문번호(mkt_orr_no)가 내려왔는지로, 조회는 기대한 Output 블록이 있는지로 판단한다.
- 유량 한도는 초당 5회 수준이라 초당 4회로 제한한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from ..config import NamuhConfig
from .base import Account, Broker, OrderResult, Position

log = logging.getLogger(__name__)

PORT = 8443
MIN_INTERVAL = 0.25  # 초당 4회


class NamuhApiError(RuntimeError):
    pass


def _num(value) -> float:
    """'1,234', '+500', ' 70000 ', '' 같은 문자열 숫자를 float로."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "").lstrip("+")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _rows(block) -> list[dict]:
    """응답 블록은 데이터가 없으면 빠지고, API에 따라 객체이거나 배열이다."""
    if not block:
        return []
    return block if isinstance(block, list) else [block]


def _symbol(code: str) -> str:
    """잔고의 종목코드(길이 12, 'A005930' 등)를 6자리로."""
    code = str(code).strip()
    m = re.search(r"(\d{6})$", code)
    return m.group(1) if m else code


def _date(value: str) -> pd.Timestamp:
    digits = re.sub(r"\D", "", str(value))
    return pd.to_datetime(digits, format="%y%m%d" if len(digits) == 6 else "%Y%m%d")


def _message(data) -> str:
    if not isinstance(data, dict):
        return str(data)[:300]
    msg = data.get("rsp_msg") or data.get("error_description") or data.get("error")
    code = data.get("rsp_cd") or data.get("error_code")
    if msg is None and code is None:
        return json.dumps(data, ensure_ascii=False)[:300]
    return f"[{code}] {msg}" if code else str(msg)


class NamuhBroker(Broker):
    def __init__(self, config: NamuhConfig, token_cache: str | Path = ".nhplug_token.json",
                 session: requests.Session | None = None):
        self.cfg = config
        live = f"https://api.{config.domain}:{PORT}"
        self.auth_url = live
        self.quote_url = live  # 시세는 운영 서버 전용
        self.trade_url = live if config.is_real else f"https://moapi.{config.domain}:{PORT}"
        self.token_cache = Path(token_cache)
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_exp = 0.0
        self._last_call = 0.0
        self._min_interval = MIN_INTERVAL

    # ---------- 인증 ----------
    def _cache_scope(self) -> str:
        return f"{self.cfg.app_key[:6]}|{self.auth_url}"

    def _access_token(self, force: bool = False) -> str:
        now = time.time()
        if not force:
            if self._token and now < self._token_exp - 60:
                return self._token
            try:
                cached = json.loads(self.token_cache.read_text())
                if cached.get("scope") == self._cache_scope() and now < cached["exp"] - 60:
                    self._token, self._token_exp = cached["token"], cached["exp"]
                    return self._token
            except (OSError, ValueError, KeyError):
                pass
        resp = self.session.post(f"{self.auth_url}/oauth2/token", params={
            "appkey": self.cfg.app_key,
            "appsecretkey": self.cfg.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob",
        }, headers={"content-type": "application/x-www-form-urlencoded"}, timeout=10)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise NamuhApiError(f"토큰 발급 실패 (HTTP {resp.status_code}): {_message(data)}")
        self._token = data["access_token"]
        self._token_exp = now + int(data.get("expires_in", 86400))
        log.info("나무증권 접근토큰 발급 (24시간 유효, 캐시: %s)", self.token_cache)
        self.token_cache.write_text(json.dumps({
            "token": self._token, "exp": self._token_exp, "scope": self._cache_scope(),
        }))
        try:
            self.token_cache.chmod(0o600)
        except OSError:
            pass
        return self._token

    # ---------- 공통 호출 ----------
    def _post(self, base_url: str, path: str, input_0: dict) -> dict:
        for attempt in range(2):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            resp = self.session.post(f"{base_url}{path}", headers={
                "authorization": f"Bearer {self._access_token(force=attempt > 0)}",
                "x-client-id": self.cfg.app_key,
                "x-client-secret": self.cfg.app_secret,
                "content-type": "application/json; charset=UTF-8",
            }, data=json.dumps({"Input_0": input_0}), timeout=10)
            self._last_call = time.monotonic()
            try:
                data = resp.json()
            except ValueError:
                data = resp.text
            # 토큰 무효일 때만 1회 재발급 (429 등에는 재발급하지 않음)
            if attempt == 0 and (resp.status_code == 401 or "IGW40043" in str(resp.text)):
                continue
            break
        if resp.status_code == 429:
            raise NamuhApiError(f"호출 유량 초과(429): {_message(data)}")
        if resp.status_code != 200:
            raise NamuhApiError(f"HTTP {resp.status_code} {path}: {_message(data)}")
        if not isinstance(data, dict):
            raise NamuhApiError(f"{path}: 예상치 못한 응답 {str(data)[:200]}")
        return data

    # ---------- 시세 ----------
    def daily_ohlcv(self, symbol: str, days: int = 120) -> pd.DataFrame:
        data = self._post(self.quote_url, "/krstock/quote/v1/period", {
            "market_cd": "KRX",
            "iem_cd": symbol,
            "gubun": "1",  # 일봉
            "edate": datetime.now().strftime("%Y%m%d"),
            "array_cnt": str(min(int(days), 9999)),
            "view_main_yn": "Y",  # 정규장 시세
        })
        rows = [r for r in _rows(data.get("Output_1")) if str(r.get("bsop_date", "")).strip()]
        if not rows:
            raise NamuhApiError(f"{symbol} 일봉 데이터가 없습니다: {_message(data)}")
        df = pd.DataFrame({
            "date": [_date(r["bsop_date"]) for r in rows],
            "open": [_num(r.get("stck_oprc")) for r in rows],
            "high": [_num(r.get("stck_hgpr")) for r in rows],
            "low": [_num(r.get("stck_lwpr")) for r in rows],
            "close": [_num(r.get("stck_prpr")) for r in rows],
            "volume": [_num(r.get("vol")) for r in rows],
        })
        df = df.drop_duplicates("date").set_index("date").sort_index()
        return df.tail(days)

    def investor_flow(self, symbol: str, days: int = 120) -> pd.DataFrame:
        """투자자별 일별 순매수량(주). 기관은 연기금·투신·보험 등을 합친 '기관계'."""
        data = self._post(self.quote_url, "/krstock/quote/v1/currentInvestor", {
            "market_cd": "KRX",
            "iem_cd": symbol,
            "array_cnt": str(min(int(days), 999)),
        })
        rows = [r for r in _rows(data.get("Output_0")) if str(r.get("bsop_date1", "")).strip()]
        if not rows:
            raise NamuhApiError(f"{symbol} 투자자 수급 데이터가 없습니다: {_message(data)}")
        df = pd.DataFrame({
            "date": [_date(r["bsop_date1"]) for r in rows],
            "foreign": [_num(r.get("frgn_ntby_qty")) for r in rows],
            "institution": [_num(r.get("gigwan")) for r in rows],
            "individual": [_num(r.get("person")) for r in rows],
            "program": [_num(r.get("program")) for r in rows],
        })
        return df.drop_duplicates("date").set_index("date").sort_index().tail(days)

    def current_price(self, symbol: str) -> float:
        data = self._post(self.quote_url, "/krstock/quote/v1/currentPrice",
                          {"market_cd": "KRX", "iem_cd": symbol})
        price = abs(_num((data.get("Output_0") or {}).get("stck_prpr")))
        if price <= 0:
            raise NamuhApiError(f"{symbol} 현재가 조회 실패: {_message(data)}")
        return price

    # ---------- 계좌 ----------
    def list_accounts(self) -> list[dict]:
        """계좌 목록. acct_type 01·02 = 운영, 03 = 모의투자."""
        data = self._post(self.trade_url, "/n2/acctinfo", {})
        return _rows(data.get("Output_0"))

    def account(self) -> Account:
        data = self._post(self.trade_url, "/krstock/inquiry/v1/balance", {
            "act_no": self.cfg.account_no,
            "bnc_bse_cd": "5",  # 현재가 기준 평가
            "ltg_aot_dit_cd": "9",  # 전체
            "aet_bse": "2",  # 총자산
            "qut_dit_cd": "KRX",
            "aly_qut_cd": "1",  # 정규장 (필수)
        })
        summary = data.get("Output_0")
        if not summary:
            raise NamuhApiError(f"잔고 조회 실패: {_message(data)}")
        summary = _rows(summary)[0]
        positions = {}
        for r in _rows(data.get("Output_1")):
            qty = int(_num(r.get("itg_bnc_qty")))
            if qty > 0:
                sym = _symbol(r.get("iem_cd", ""))
                positions[sym] = Position(sym, qty, _num(r.get("phs_pr")), _num(r.get("now_pr")))
        # D+2 예수금(결제 예정분 반영)을 주문 가능 현금으로 사용
        cash = _num(summary.get("nxt2_dd_dca")) or _num(summary.get("dca"))
        return Account(cash=cash, positions=positions)

    def buyable_quantity(self, symbol: str, price: float = 0) -> int:
        """미수 없이 현금으로 살 수 있는 수량."""
        input_0 = {
            "ost_dit_cd": "1",  # 현금
            "act_no": self.cfg.account_no,
            "iem_cd": symbol,
            "nmn_pr_tp_cd": "01" if price else "05",
        }
        if price:
            input_0["orr_pr"] = int(price)
        data = self._post(self.trade_url, "/krstock/inquiry/v1/buyableQuantity", input_0)
        out = data.get("Output_0")
        if not out:
            raise NamuhApiError(f"{symbol} 매수가능수량 조회 실패: {_message(data)}")
        return int(_num(_rows(out)[0].get("csh_orr_pbl_qty")))

    def sellable_quantity(self, symbol: str) -> int:
        data = self._post(self.trade_url, "/krstock/inquiry/v1/sellableQuantity", {
            "act_no": self.cfg.account_no,
            "iem_cd": symbol,  # 명세상 선택이지만 실제로는 필수
            "cfd_lon_cd": "00",
        })
        out = data.get("Output_0")
        if not out:
            raise NamuhApiError(f"{symbol} 매도가능수량 조회 실패: {_message(data)}")
        return int(_num(_rows(out)[0].get("sll_pbl_qty")))

    # ---------- 주문 ----------
    def _order(self, side: str, symbol: str, qty: int, price: float) -> OrderResult:
        try:
            limit = self.buyable_quantity(symbol, price) if side == "buy" else self.sellable_quantity(symbol)
        except NamuhApiError as e:
            return OrderResult(symbol, side, qty, price, ok=False, message=str(e))
        if limit < qty:
            log.warning("%s %s: 요청 %d주 > 가능 %d주, 가능 수량으로 조정", symbol, side, qty, limit)
            qty = limit
        if qty <= 0:
            return OrderResult(symbol, side, 0, price, ok=False, message="주문 가능 수량 0")
        input_0 = {
            "act_no": self.cfg.account_no,
            "iem_cd": symbol,
            "orr_qty": int(qty),
            "nmn_pr_tp_cd": "01" if price else "05",  # 01 보통가(지정가) / 05 시장가
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if price:
            input_0["orr_pr"] = int(price)
        path = "/krstock/order/v1/cashBuy" if side == "buy" else "/krstock/order/v1/cashSell"
        try:
            data = self._post(self.trade_url, path, input_0)
        except NamuhApiError as e:
            return OrderResult(symbol, side, qty, price, ok=False, message=str(e))
        order_no = str((_rows(data.get("Output_0")) or [{}])[0].get("mkt_orr_no", "")).strip()
        return OrderResult(symbol, side, qty, price, order_id=order_no, ok=bool(order_no),
                           message=_message(data))

    def buy(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        return self._order("buy", symbol, qty, price)

    def sell(self, symbol: str, qty: int, price: float = 0) -> OrderResult:
        return self._order("sell", symbol, qty, price)
