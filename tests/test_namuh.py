import json

import pytest

from trading_bot.broker.namuh import NamuhBroker, _num, _symbol
from trading_bot.config import NamuhConfig


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.text = json.dumps(data, ensure_ascii=False)

    def json(self):
        return self.data


class FakeServer:
    """명세(openapi.json)의 요청/응답 형태를 흉내내는 가짜 세션."""

    def __init__(self):
        self.calls = []
        self.token_issued = 0
        self.expire_token_once = False
        self.buyable = 1000
        self.sellable = 1000
        self.today_bar = []
        self.order_resp = {"rsp_cd": "00000", "rsp_msg": "주문이 완료되었습니다.",
                           "Output_0": {"mkt_orr_no": "0000012345"}}

    def post(self, url, params=None, headers=None, data=None, timeout=None):
        if url.endswith("/oauth2/token"):
            self.token_issued += 1
            assert params["grant_type"] == "client_credentials" and params["scope"] == "oob"
            return Resp({"access_token": f"tok{self.token_issued}", "expires_in": 86400})
        body = json.loads(data)["Input_0"]
        self.calls.append((url, headers, body))
        if self.expire_token_once:
            self.expire_token_once = False
            return Resp({"error_code": "IGW40043", "error_description": "유효하지 않은 token"}, 401)
        if url.endswith("/krstock/quote/v1/period"):
            return Resp({"Output_0": {"iem_cd": body["iem_cd"]}, "Output_1": self.today_bar + [
                {"bsop_date": "20261016", "stck_oprc": "100", "stck_hgpr": "110", "stck_lwpr": "90",
                 "stck_prpr": "105", "vol": "1,000"},
                {"bsop_date": "20261015", "stck_oprc": "95", "stck_hgpr": "101", "stck_lwpr": "94",
                 "stck_prpr": "100", "vol": "900"},
            ]})
        if url.endswith("/currentPrice"):
            return Resp({"Output_0": {"stck_prpr": "71000"}})
        if url.endswith("/balance"):
            return Resp({"Output_0": {"dca": "300000", "nxt2_dd_dca": "250,000"}, "Output_1": [
                {"iem_cd": "A005930", "itg_bnc_qty": "3.000000", "phs_pr": "70000", "now_pr": "71000"},
                {"iem_cd": "000660", "itg_bnc_qty": "0", "phs_pr": "0", "now_pr": "0"},
            ]})
        if url.endswith("/buyableQuantity"):
            return Resp({"Output_0": {"csh_orr_pbl_qty": str(self.buyable)}})
        if url.endswith("/sellableQuantity"):
            return Resp({"Output_0": {"sll_pbl_qty": str(self.sellable), "bnc_qty": "3"}})
        if url.endswith(("/cashBuy", "/cashSell")):
            return Resp(self.order_resp)
        if url.endswith("/n2/acctinfo"):
            return Resp({"Output_0": [{"acct_no": "12345678901", "acct_type": "03"}]})
        return Resp({"rsp_cd": "99999", "rsp_msg": "unknown"}, 404)


def make(tmp_path, mode="paper"):
    cfg = NamuhConfig("key", "secret", "12345678901", mode)
    b = NamuhBroker(cfg, token_cache=tmp_path / "tok.json", session=FakeServer())
    b._min_interval = 0
    return b


def test_hosts_paper_mode(tmp_path):
    b = make(tmp_path)
    b.current_price("005930")
    b.account()
    urls = [c[0] for c in b.session.calls]
    # 시세는 운영 서버(모의투자 미제공), 계좌/주문은 모의투자 서버
    assert urls[0].startswith("https://api.nhplug.com:8443/krstock/quote")
    assert urls[1].startswith("https://moapi.nhplug.com:8443/krstock/inquiry")


def test_hosts_real_mode(tmp_path):
    b = make(tmp_path, "real")
    b.account()
    assert b.session.calls[0][0].startswith("https://api.nhplug.com:8443")


def test_headers_and_envelope(tmp_path):
    b = make(tmp_path)
    b.current_price("005930")
    _, headers, body = b.session.calls[0]
    assert headers["authorization"] == "Bearer tok1"
    assert headers["x-client-id"] == "key" and headers["x-client-secret"] == "secret"
    assert body == {"market_cd": "KRX", "iem_cd": "005930"}


def test_daily_ohlcv(tmp_path):
    b = make(tmp_path)
    df = b.daily_ohlcv("005930", days=2)
    assert list(df["close"]) == [100.0, 105.0]
    assert df["volume"].iloc[-1] == 1000
    body = b.session.calls[0][2]
    assert body["gubun"] == "1" and body["view_main_yn"] == "Y"


def test_account(tmp_path):
    acc = make(tmp_path).account()
    assert acc.cash == 250000
    assert list(acc.positions) == ["005930"]
    p = acc.positions["005930"]
    assert (p.qty, p.avg_price, p.current_price) == (3, 70000, 71000)


def test_market_buy_payload(tmp_path):
    b = make(tmp_path)
    res = b.buy("005930", 2)
    assert res.ok and res.order_id == "0000012345"
    url, _, body = b.session.calls[-1]
    assert url.endswith("/krstock/order/v1/cashBuy")
    assert body["nmn_pr_tp_cd"] == "05" and "orr_pr" not in body
    assert body["orr_qty"] == 2 and body["rmt_mkt_cd"] == "KRX" and body["sor_mkt_sli_yn"] == "N"


def test_limit_sell_payload(tmp_path):
    b = make(tmp_path)
    b.sell("005930", 1, price=72000)
    url, _, body = b.session.calls[-1]
    assert url.endswith("/cashSell")
    assert body["nmn_pr_tp_cd"] == "01" and body["orr_pr"] == 72000


def test_qty_capped_by_buyable(tmp_path):
    b = make(tmp_path)
    b.session.buyable = 1
    res = b.buy("005930", 5)
    assert res.ok and res.qty == 1
    assert b.session.calls[-1][2]["orr_qty"] == 1


def test_zero_buyable_skips_order(tmp_path):
    b = make(tmp_path)
    b.session.buyable = 0
    res = b.buy("005930", 5)
    assert not res.ok
    assert not any(c[0].endswith("/cashBuy") for c in b.session.calls)


def test_order_without_order_number_is_failure(tmp_path):
    b = make(tmp_path)
    b.session.order_resp = {"rsp_cd": "12345", "rsp_msg": "주문가능금액이 부족합니다."}
    res = b.buy("005930", 1)
    assert not res.ok and "부족" in res.message


def test_token_cached_across_instances(tmp_path):
    b = make(tmp_path)
    b.current_price("005930")
    b2 = make(tmp_path)
    b2.current_price("005930")
    assert b2.session.token_issued == 0


def test_token_reissued_on_401(tmp_path):
    b = make(tmp_path)
    b.current_price("005930")
    b.session.expire_token_once = True
    assert b.current_price("005930") == 71000
    assert b.session.token_issued == 2


def test_list_accounts(tmp_path):
    assert make(tmp_path).list_accounts()[0]["acct_type"] == "03"


def test_helpers():
    assert _num("1,234") == 1234 and _num("+5") == 5 and _num("") == 0 and _num(None) == 0
    assert _symbol("A005930") == "005930" and _symbol("005930") == "005930"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("NHPLUG_APP_KEY", "k")
    monkeypatch.setenv("NHPLUG_APP_SECRET", "s")
    monkeypatch.setenv("NHPLUG_ACCOUNT", "123-45678-901")
    monkeypatch.setenv("NHPLUG_MODE", "paper")
    cfg = NamuhConfig.from_env()
    assert cfg.account_no == "12345678901" and not cfg.is_real
    monkeypatch.setenv("NHPLUG_MODE", "live")
    with pytest.raises(ValueError):
        NamuhConfig.from_env()


def test_bot_trades_through_namuh(tmp_path):
    from datetime import datetime

    import pandas as pd

    from trading_bot.bot import KST, TradingBot
    from trading_bot.config import BotConfig, RiskConfig

    class Always:
        name, warmup = "on", 1

        def signals(self, df):
            return pd.Series(1, index=df.index)

    b = make(tmp_path)
    b.session.today_bar = [{"bsop_date": "20261019", "stck_oprc": "105", "stck_hgpr": "105",
                            "stck_lwpr": "105", "stck_prpr": "105", "vol": "1"}]
    cfg = BotConfig(symbols=["000660"], risk=RiskConfig(max_position_pct=0.5))
    bot = TradingBot(b, Always(), cfg, state_path=tmp_path / "s.json")
    bot.step(datetime(2026, 10, 19, 9, 10, tzinfo=KST))
    buys = [c for c in b.session.calls if c[0].endswith("/cashBuy")]
    assert len(buys) == 1 and buys[0][2]["iem_cd"] == "000660"
    # 총자산(250,000 + 3*71,000) * 50% / 71,000 = 3주
    assert buys[0][2]["orr_qty"] == 3
