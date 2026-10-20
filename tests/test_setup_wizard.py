from trading_bot.setup_wizard import Wizard, read_env, save_env


class FakeTg:
    sent = []

    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def get_updates(self, timeout=0):
        return [{"update_id": 1, "message": {"chat": {"id": 4242}, "text": "hi"}}]

    def send(self, text):
        FakeTg.sent.append((self.chat_id, text))


class FakeBroker:
    def list_accounts(self):
        return [{"acct_no": "11111111101", "acct_type": "01"},
                {"acct_no": "33333333303", "acct_type": "03"}]


def run(tmp_path, answers, monkeypatch):
    for k in ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NHPLUG_MODE", "NHPLUG_ACCOUNT",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BOT_ARGS", "NHPLUG_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    it = iter(answers)
    w = Wizard(tmp_path / ".env", ask=lambda _: next(it), telegram_factory=FakeTg,
               broker_factory=FakeBroker)
    return w.run(), read_env(tmp_path / ".env")


def test_full_paper_setup(tmp_path, monkeypatch):
    code, env = run(tmp_path, ["KEY", "SECRET", "", "TGTOKEN", "", "005930 035420", "", "", ""], monkeypatch)
    assert code == 0
    assert env["NHPLUG_APP_KEY"] == "KEY" and env["NHPLUG_MODE"] == "paper"
    assert env["NHPLUG_ACCOUNT"] == "33333333303"  # 모의투자(03) 계좌 자동 선택
    assert env["TELEGRAM_CHAT_ID"] == "4242"
    assert env["BOT_ARGS"].startswith("--symbols 005930 035420 --strategy breakout")
    assert "--confirm-real" not in env["BOT_ARGS"]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_rerun_keeps_values(tmp_path, monkeypatch):
    run(tmp_path, ["KEY", "SECRET", "", "TGTOKEN", "", "", "", "3", ""], monkeypatch)
    code, env = run(tmp_path, ["", "", "", "", "", "", "", "", ""], monkeypatch)
    assert "--strategy sma " in env["BOT_ARGS"] + " "  # 기존 선택 유지
    assert code == 0 and env["NHPLUG_APP_KEY"] == "KEY" and env["TELEGRAM_CHAT_ID"] == "4242"


def test_real_requires_confirmation(tmp_path, monkeypatch):
    code, env = run(tmp_path, ["K", "S", "2", "아니", "", "", "", "", ""], monkeypatch)
    assert env["NHPLUG_MODE"] == "paper"
    code, env = run(tmp_path, ["", "", "2", "실전", "", "", "", "", "0.5"], monkeypatch)
    assert env["NHPLUG_MODE"] == "real" and env["NHPLUG_ACCOUNT"] == "11111111101"
    assert "--max-position 0.5" in env["BOT_ARGS"]
    assert env["BOT_ARGS"].endswith("--confirm-real")


def test_save_env_preserves_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# 주석\nA=1\nB=2\n")
    save_env(p, {"B": "3", "C": "4"})
    assert p.read_text() == "# 주석\nA=1\nB=3\nC=4\n"


def test_network_error_message(tmp_path, monkeypatch, capsys):
    import requests

    class Down:
        def list_accounts(self):
            raise requests.ConnectionError("reset")

    for k in ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    it = iter(["K", "S", ""])
    w = Wizard(tmp_path / ".env", ask=lambda _: next(it), broker_factory=Down)
    assert w.run() == 1
    assert "네트워크" in capsys.readouterr().out


def test_wrong_telegram_account_rejected(tmp_path, monkeypatch):
    code, env = run(tmp_path, ["KEY", "SECRET", "", "TGTOKEN", "n"], monkeypatch)
    assert code == 1 and "TELEGRAM_CHAT_ID" not in env


def test_split_mode_setup(tmp_path, monkeypatch):
    code, env = run(tmp_path, ["K", "S", "", "", "", "2", "1", "0.6", "2", ""], monkeypatch)
    assert code == 0
    args = env["BOT_ARGS"]
    assert "--strategy breakout" in args and "--core-strategy dual_momentum" in args
    assert "--core-weight 0.6" in args and "--core-param top_n=3" in args
    assert "--core-symbols 005930 000660 005380" in args
    # 다시 실행하면 나눠서 운용이 기본값으로 유지
    code, env2 = run(tmp_path, ["", "", "", "", "", "", "", "", "", ""], monkeypatch)
    assert env2["BOT_ARGS"] == args
    # BOT_ARGS 가 실제 실행 옵션으로 파싱되는지
    from trading_bot.__main__ import build_parser
    ns = build_parser().parse_args(["run", "--broker", "api", *args.split()])
    assert ns.core_symbols[:2] == ["005930", "000660"] and ns.core_weight == 0.6


def test_split_mode_auto_universe(tmp_path, monkeypatch):
    code, env = run(tmp_path, ["K", "S", "", "", "", "2", "1", "", "", "15"], monkeypatch)
    assert code == 0 and "--core-top 15" in env["BOT_ARGS"]
    code, env2 = run(tmp_path, [""] * 10, monkeypatch)
    assert env2["BOT_ARGS"] == env["BOT_ARGS"]
