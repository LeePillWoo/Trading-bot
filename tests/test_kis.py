from trading_bot.broker.kis import KisBroker
from trading_bot.config import KisConfig


class FakeResp:
    def __init__(self, data):
        self.data, self.status_code, self.text = data, 200, ""

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, None, json))
        return FakeResp({"access_token": "tok", "expires_in": 86400})

    def request(self, method, url, headers, params, json, timeout):
        self.calls.append((method, url, headers, json or params))
        if "order-cash" in url:
            return FakeResp({"rt_cd": "0", "msg1": "ok", "output": {"ODNO": "123"}})
        if "inquire-balance" in url:
            return FakeResp({"rt_cd": "0", "output1": [
                {"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "70000.0", "prpr": "71000"}],
                "output2": [{"prvs_rcdl_excc_amt": "500000", "dnca_tot_amt": "600000"}]})
        if "itemchartprice" in url:
            if params["FID_INPUT_DATE_2"] < "2026":
                return FakeResp({"rt_cd": "0", "output2": []})
            return FakeResp({"rt_cd": "0", "output2": [
                {"stck_bsop_date": d, "stck_oprc": "1", "stck_hgpr": "2", "stck_lwpr": "1",
                 "stck_clpr": c, "acml_vol": "10"} for d, c in (("20261016", "2"), ("20261015", "1"))]})
        return FakeResp({"rt_cd": "1", "msg_cd": "X", "msg1": "error"})


def broker(tmp_path, mode="paper"):
    cfg = KisConfig("key", "secret", "12345678", "01", mode)
    b = KisBroker(cfg, token_cache=tmp_path / "t.json", session=FakeSession())
    b._min_interval = 0
    return b


def test_paper_order_uses_mock_tr_id(tmp_path):
    b = broker(tmp_path)
    res = b.buy("005930", 2)
    assert res.ok and res.order_id == "123"
    _, url, headers, body = b.session.calls[-1]
    assert url.startswith("https://openapivts")
    assert headers["tr_id"] == "VTTC0012U"
    assert body["ORD_DVSN"] == "01" and body["ORD_QTY"] == "2"


def test_real_sell_limit_order(tmp_path):
    b = broker(tmp_path, "real")
    b.sell("005930", 1, price=70000)
    _, url, headers, body = b.session.calls[-1]
    assert headers["tr_id"] == "TTTC0011U"
    assert body["ORD_DVSN"] == "00" and body["ORD_UNPR"] == "70000"


def test_account_and_token_cache(tmp_path):
    b = broker(tmp_path)
    acc = b.account()
    assert acc.cash == 500000
    assert acc.positions["005930"].qty == 3
    # 두 번째 인스턴스는 캐시된 토큰을 재사용
    b2 = broker(tmp_path)
    b2.account()
    assert not any(c[0] == "POST" for c in b2.session.calls)


def test_daily_ohlcv_sorted(tmp_path):
    df = broker(tmp_path).daily_ohlcv("005930", days=10)
    assert list(df["close"]) == [1.0, 2.0]
    assert df.index.is_monotonic_increasing


def test_api_error_returns_failed_order(tmp_path):
    b = broker(tmp_path)
    b.session.request = lambda *a, **k: FakeResp({"rt_cd": "1", "msg_cd": "E1", "msg1": "잔고부족"})
    res = b.buy("005930", 1)
    assert not res.ok and "잔고부족" in res.message
