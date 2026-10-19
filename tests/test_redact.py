import logging

import requests

from trading_bot.redact import RedactingFormatter, redact


def test_redacts_env_secrets_and_patterns(monkeypatch):
    monkeypatch.setenv("NHPLUG_APP_KEY", "MYAPPKEY123")
    monkeypatch.setenv("NHPLUG_APP_SECRET", "MYSECRET456")
    text = ("url: /oauth2/token?appkey=MYAPPKEY123&appsecretkey=MYSECRET456 "
            "/bot7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates")
    out = redact(text)
    assert "MYAPPKEY123" not in out and "MYSECRET456" not in out
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out


def test_formatter_redacts_tracebacks(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    try:
        raise RuntimeError("failed /bot7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/x")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, __import__("sys").exc_info())
    assert "AAHdq" not in RedactingFormatter("%(message)s").format(record)


def test_namuh_token_network_error_hides_keys(tmp_path):
    from trading_bot.broker.namuh import NamuhApiError, NamuhBroker
    from trading_bot.config import NamuhConfig

    class Down:
        def post(self, url, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}?appkey=KEYKEYKEY&appsecretkey=SECSECSEC")

    b = NamuhBroker(NamuhConfig("KEYKEYKEY", "SECSECSEC", "1"), token_cache=tmp_path / "t.json", session=Down())
    try:
        b.current_price("005930")
    except NamuhApiError as e:
        assert "KEYKEYKEY" not in str(e) and "SECSECSEC" not in str(e)
        assert e.__cause__ is None and e.__suppress_context__
    else:
        raise AssertionError("예외가 나야 함")


def test_telegram_failure_log_has_no_token(caplog):
    from trading_bot.notify import TelegramNotifier

    class Down:
        def post(self, url, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

        def get(self, url, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    tg = TelegramNotifier("7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", "1", session=Down())
    with caplog.at_level(logging.WARNING):
        tg.send("hi")
        tg.get_updates(0)
    assert "AAHdq" not in caplog.text
