"""CLI.

예시:
  python -m trading_bot service                                    # .env 의 BOT_ARGS 로 실행 (Termux/서버)
  python -m trading_bot setup                                      # 대화형 설정 (키 입력, 계좌/텔레그램 자동 확인)
  python -m trading_bot telegram-setup                             # 텔레그램 chat_id 확인
  python -m trading_bot accounts                                   # 나무증권 계좌 목록/구분 확인
  python -m trading_bot backtest --demo
  python -m trading_bot backtest --csv data/005930.csv --strategy rsi
  python -m trading_bot backtest --fetch --symbols 005930 000660 --days 500
  python -m trading_bot run --symbols 005930 000660                # 로컬 가상매매 (실시세, 주문 없음)
  python -m trading_bot run --broker api --symbols 005930          # 증권사 계좌로 주문 (모의투자/실전)
  python -m trading_bot run --api kis --broker api                 # 한국투자증권 사용
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
import time
from datetime import time as dtime
from pathlib import Path

from .backtest import run_backtest
from .config import BotConfig, KisConfig, NamuhConfig, RiskConfig, load_dotenv
from .data import load_csv, synthetic_ohlcv
from .notify import Notifier, TelegramNotifier
from .strategy import STRATEGIES, make_strategy

APIS = ("namuh", "kis")


def setup_logging(log_dir: str = "logs") -> None:
    from logging.handlers import RotatingFileHandler

    from .redact import RedactingFormatter

    Path(log_dir).mkdir(exist_ok=True)
    # 로그 파일은 1MB 씩 최대 4개까지만 보관, 비밀값은 가려서 기록
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(Path(log_dir) / "trading.log", maxBytes=1_000_000,
                                    backupCount=3, encoding="utf-8")]
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def parse_params(pairs: list[str]) -> dict:
    params = {}
    for pair in pairs or []:
        key, value = pair.split("=", 1)
        params[key] = float(value) if "." in value else int(value)
    return params


def make_api_broker(api: str, require_account: bool = True):
    """증권사 API 브로커와 설정을 만든다."""
    if api == "namuh":
        from .broker.namuh import NamuhBroker
        cfg = NamuhConfig.from_env()
        if require_account and not cfg.account_no:
            raise ValueError("NHPLUG_ACCOUNT 가 설정되지 않았습니다 "
                             "(python -m trading_bot accounts 로 계좌번호 확인)")
        return NamuhBroker(cfg), cfg
    from .broker.kis import KisBroker
    cfg = KisConfig.from_env()
    return KisBroker(cfg), cfg


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading_bot", description="주식 자동매매 봇")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--api", choices=APIS, default="namuh",
                        help="증권사 API: namuh(나무증권/NH투자증권, 기본) | kis(한국투자증권)")
        sp.add_argument("--strategy", default="sma_flow", choices=sorted(STRATEGIES),
                        help="sma_flow(이동평균+수급, 기본) | sma | rsi | breakout")
        sp.add_argument("--param", nargs="*", default=[], metavar="KEY=VALUE",
                        help="전략 파라미터 (예: short=5 long=20)")
        sp.add_argument("--symbols", nargs="+", default=["005930"])
        sp.add_argument("--max-position", type=float, default=0.2, help="종목당 최대 비중")
        sp.add_argument("--stop-loss", type=float, default=0.07, help="손절 비율 (0이면 미사용)")
        sp.add_argument("--take-profit", type=float, default=0.0, help="익절 비율 (0이면 미사용)")
        sp.add_argument("--max-daily-loss", type=float, default=0.03,
                        help="하루 손실 한도, 도달하면 일시정지 (0이면 미사용)")
        sp.add_argument("--cash", type=float, default=10_000_000, help="초기 자금 (백테스트/가상매매)")

    sub.add_parser("setup", help="대화형 설정 마법사 (.env 생성)")
    sub.add_parser("telegram-setup", help="텔레그램 봇 연결 확인 및 chat_id 찾기")

    acc = sub.add_parser("accounts", help="나무증권 계좌 목록과 사용 가능 환경 확인")
    acc.set_defaults(api="namuh")

    bt = sub.add_parser("backtest", help="과거 데이터로 전략 검증")
    common(bt)
    src = bt.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", nargs="+", help="종목별 CSV 파일 (파일명=종목코드)")
    src.add_argument("--fetch", action="store_true", help="증권사 API(--api)로 일봉 조회")
    src.add_argument("--demo", action="store_true", help="랜덤 데이터로 데모 실행")
    bt.add_argument("--days", type=int, default=500)
    bt.add_argument("--compare", action="store_true", help="모든 전략을 같은 데이터로 비교")

    run = sub.add_parser("run", help="자동매매 실행")
    common(run)
    run.add_argument("--broker", choices=["paper", "api"], default="paper",
                     help="paper: 로컬 가상매매(실시세, 주문 없음) | api: 증권사 계좌로 주문")
    run.add_argument("--interval", type=int, default=300, help="장중 점검 주기(초)")
    run.add_argument("--trade-time", default="09:05", help="일일 리밸런싱 시각 (HH:MM, KST)")
    run.add_argument("--once", action="store_true", help="한 번만 실행 (cron 용)")
    run.add_argument("--confirm-real", action="store_true",
                     help="실전 모드(NHPLUG_MODE/KIS_MODE=real)에서 주문을 허용하려면 반드시 지정")
    return p


def cmd_accounts() -> int:
    broker, cfg = make_api_broker("namuh", require_account=False)
    env = "real" if cfg.is_real else "paper"
    want = {"real": ("01", "02"), "paper": ("03",)}[env]
    labels = {"01": "운영(일반)", "02": "운영(주문대리인)", "03": "모의투자"}
    print(f"현재 모드: {env} ({broker.trade_url})")
    for a in broker.list_accounts():
        t = str(a.get("acct_type", "")).strip()
        mark = "✅ 사용 가능" if t in want else "❌ 다른 모드용"
        print(f"  {a.get('acct_no')}  {labels.get(t, t or '?'):12s} {mark}")
    print("\n사용할 계좌번호를 .env 의 NHPLUG_ACCOUNT 에 넣으세요.")
    return 0


def make_notifier() -> Notifier:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    logging.getLogger("trading_bot").warning(
        "텔레그램 미설정 (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — 알림 없이 실행합니다")
    return Notifier()


def cmd_telegram_setup() -> int:
    from .setup_wizard import find_chat_id
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN 이 설정되지 않았습니다 (@BotFather 에서 발급)")
    tg = TelegramNotifier(token, "")
    print("텔레그램에서 내 봇에게 아무 메시지나 보내세요. (최대 2분 대기)")
    found = find_chat_id(tg)
    if not found:
        print("메시지를 받지 못했습니다. 봇 토큰과, 봇에게 메시지를 보냈는지 확인하세요.")
        return 1
    chat_id, name = found
    tg.chat_id = chat_id
    print(f"\n보낸 계정: {name} (본인이 아니면 등록하지 마세요)")
    print(f"chat_id 를 찾았습니다: {chat_id}\n.env 에 TELEGRAM_CHAT_ID={chat_id} 를 넣으세요.")
    tg.send("✅ 자동매매 봇 연결 확인 완료")
    return 0


def service_argv() -> list[str]:
    """`service` = .env 의 BOT_ARGS 로 증권사 계좌 자동매매 실행 (Termux/서버 공용)."""
    load_dotenv()
    return ["run", "--broker", "api", *shlex.split(os.environ.get("BOT_ARGS", ""))]


def cmd_backtest(args, strategy, risk, cfg) -> int:
    from .strategy import with_flow

    if args.csv:
        data = {Path(f).stem: load_csv(f) for f in args.csv}
    elif args.fetch:
        api, _ = make_api_broker(args.api, require_account=False)
        data = {}
        for s in args.symbols:
            df = api.daily_ohlcv(s, args.days)
            try:
                df = with_flow(df, api.investor_flow(s, args.days))
            except Exception as e:
                print(f"({s} 수급 데이터 없음: {e})")
            data[s] = df
    else:
        data = {s: synthetic_ohlcv(args.days, seed=i) for i, s in enumerate(args.symbols)}
    has_flow = all("foreign" in df.columns for df in data.values())

    def run(strat):
        return run_backtest(data, strat, cash=args.cash, risk=risk,
                            commission=cfg.commission, sell_tax=cfg.sell_tax).stats()

    if args.compare:
        rows = []
        for name in STRATEGIES:
            strat = make_strategy(name)
            if strat.needs_flow and not has_flow:
                continue
            rows.append((name, run(strat)))
        first = rows[0][1]
        print(f"기간 {first['기간']} · 종목 {', '.join(data)}\n")
        print(f"{'전략':<10}{'수익률':>9}{'MDD':>9}{'거래':>6}{'승률':>8}")
        for name, st in rows:
            print(f"{name:<10}{st['총수익률']:>9.1%}{st['최대낙폭(MDD)']:>9.1%}{st['거래횟수']:>6}{st['승률']:>8.0%}")
        print(f"{'단순보유':<10}{first['단순보유수익률']:>9.1%}{first['단순보유MDD']:>9.1%}")
        return 0

    if strategy.needs_flow and not has_flow:
        raise ValueError(f"{strategy.name} 전략은 투자자 수급 데이터가 필요합니다 (--fetch 또는 --demo)")
    for k, v in run(strategy).items():
        if k == "샤프지수":
            print(f"{k:>14}: {v:.2f}")
        elif isinstance(v, float):
            print(f"{k:>14}: {v:.2%}")
        else:
            print(f"{k:>14}: {v:,}" if isinstance(v, int) else f"{k:>14}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["service"]:
        argv = service_argv()
    args = build_parser().parse_args(argv)
    load_dotenv()
    setup_logging()
    log = logging.getLogger("trading_bot")

    if args.command == "accounts":
        return cmd_accounts()
    if args.command == "setup":
        from .setup_wizard import Wizard
        return Wizard().run()
    if args.command == "telegram-setup":
        return cmd_telegram_setup()

    strategy = make_strategy(args.strategy, **parse_params(args.param))
    risk = RiskConfig(max_position_pct=args.max_position, stop_loss_pct=args.stop_loss,
                      take_profit_pct=args.take_profit, max_daily_loss_pct=args.max_daily_loss)
    cfg = BotConfig(symbols=args.symbols, strategy=args.strategy, risk=risk)

    if args.command == "backtest":
        return cmd_backtest(args, strategy, risk, cfg)

    # run
    from .bot import TradingBot
    from .broker.paper import PaperBroker

    api, api_cfg = make_api_broker(args.api, require_account=args.broker == "api")
    mode = "real" if api_cfg.is_real else "paper"
    if args.broker == "api":
        if api_cfg.is_real and not args.confirm_real:
            log.error("실전 모드입니다. 실제 주문을 하려면 --confirm-real 옵션을 지정하세요.")
            return 2
        broker = api
        log.warning("%s %s 계좌로 주문합니다", args.api, "실전" if api_cfg.is_real else "모의투자")
    else:
        broker = PaperBroker(cash=args.cash, data_source=api,
                             commission=cfg.commission, sell_tax=cfg.sell_tax)
        log.info("로컬 가상매매 모드 (실제 주문 없음, 상태는 프로세스 종료 시 초기화)")

    cfg.interval_sec = args.interval
    hh, mm = map(int, args.trade_time.split(":"))
    state_path = f"bot_state_{args.broker}_{args.api}_{mode}.json"
    label = {"paper": "모의투자", "real": "실전"}[mode] if args.broker == "api" else "가상매매"
    bot = TradingBot(broker, strategy, cfg, state_path=state_path, trade_time=dtime(hh, mm),
                     notifier=make_notifier(), label=label)
    if args.once:
        bot.step()
        acc = broker.account()
        log.info("계좌: 현금=%.0f 평가=%.0f 보유=%s", acc.cash, acc.equity,
                 json.dumps({s: p.qty for s, p in acc.positions.items()}))
    else:
        bot.run_forever()
    return 0


def cli() -> int:
    try:
        return main()
    except ValueError as e:
        print(f"오류: {e}\n.env.example 을 참고해 .env 파일을 설정하세요.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
