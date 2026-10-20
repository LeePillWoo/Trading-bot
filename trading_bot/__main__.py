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
        sp.add_argument("--core-strategy", choices=sorted(STRATEGIES),
                        help="장기 전략을 따로 운용 (예: dual_momentum). 지정하면 계좌를 장기/적극으로 나눔")
        sp.add_argument("--core-symbols", nargs="+", help="장기 전략 후보 종목")
        sp.add_argument("--core-weight", type=float, default=0.7, help="장기 전략 비중 (기본 0.7)")
        sp.add_argument("--core-top", type=int, default=0,
                        help="장기 후보를 코스피 시가총액 상위 N 종목으로 매달 자동 갱신 (0이면 --core-symbols 고정)")
        sp.add_argument("--core-param", nargs="*", default=[], metavar="KEY=VALUE", help="장기 전략 파라미터")
        sp.add_argument("--slippage", type=float, default=0.0,
                        help="백테스트 체결 불리함 (예: 0.001 = 매수/매도 각 0.1%%)")
        sp.add_argument("--sell-tax", type=float, default=0.002,
                        help="매도 시 거래세 (주식 0.002, 국내 ETF 는 0)")

    sub.add_parser("setup", help="대화형 설정 마법사 (.env 생성)")
    sub.add_parser("export-state", help="기기 이사용: 봇 상태를 한 줄로 출력 (키·토큰 없음)")
    im = sub.add_parser("import-state", help="기기 이사용: export 한 줄을 붙여넣어 상태 가져오기")
    im.add_argument("text", nargs="?", help="TBSTATE:... (생략하면 붙여넣기 입력)")
    st = sub.add_parser("stocks", help="1주 가격이 싼 코스피 대형주 찾기 (소액용)")
    st.add_argument("--max-price", type=float, default=30000, help="1주 최대 가격 (기본 3만원)")
    st.add_argument("--top", type=int, default=200, help="코스피 시총 상위 몇 개 안에서 찾을지")
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
    run.add_argument("--tick", type=int, default=0,
                     help="장중 시세 기록·급등 알림 주기(초). 0이면 끔. 예: 60")
    run.add_argument("--watch", nargs="*", default=[], metavar="CODE",
                     help="매매 종목 외에 시세를 기록하고 급등을 감시할 종목코드")
    run.add_argument("--spike-vol", type=float, default=5.0, help="급등 알림: 거래량이 평소의 몇 배 이상 (기본 5)")
    run.add_argument("--spike-pct", type=float, default=0.03, help="급등 알림: 전일 대비 상승률 (기본 0.03)")
    run.add_argument("--scalp-budget", type=float, default=0.0,
                     help="승인형 단타 켜기: 1회 매수 예산 (총자산 대비, 예 0.1). 0이면 끔. --tick 필요")
    run.add_argument("--scalp-take", type=float, default=0.03, help="단타 목표 수익 (기본 0.03)")
    run.add_argument("--scalp-stop", type=float, default=0.015, help="단타 손절 (기본 0.015)")
    run.add_argument("--scalp-trail", type=float, default=0.015, help="단타 고점 대비 하락 시 정리 (기본 0.015)")
    run.add_argument("--scalp-max-trades", type=int, default=5, help="단타 하루 최대 횟수 (기본 5)")
    run.add_argument("--scalp-max-loss", type=float, default=0.02, help="단타 하루 손실 한도, 총자산 대비 (기본 0.02)")
    run.add_argument("--scalp-close", default="15:20", help="단타 보유분 강제 정리 시각 (기본 15:20)")
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


def load_data(args, symbols: list[str]) -> dict:
    from .strategy import with_flow

    if args.csv:
        return {Path(f).stem: load_csv(f) for f in args.csv}
    if args.fetch:
        api, _ = make_api_broker(args.api, require_account=False)
        data = {}
        for s in symbols:
            df = api.daily_ohlcv(s, args.days)
            try:
                df = with_flow(df, api.investor_flow(s, args.days))
            except Exception as e:
                print(f"({s} 수급 데이터 없음: {e})")
            data[s] = df
        return data
    return {s: synthetic_ohlcv(args.days, seed=i) for i, s in enumerate(symbols)}


def print_stats(stats: dict) -> None:
    for k, v in stats.items():
        if k == "샤프지수":
            print(f"{k:>14}: {v:.2f}")
        elif isinstance(v, float):
            print(f"{k:>14}: {v:.2%}")
        else:
            print(f"{k:>14}: {v:,}" if isinstance(v, int) else f"{k:>14}: {v}")


def cmd_backtest_sleeves(args, strategy, risk, cfg) -> int:
    """장기/적극 나눠 운용: 각 전략을 자기 비중만큼의 자금으로 따로 돌려 합친다."""
    from .backtest import BacktestResult, _benchmark

    core = make_strategy(args.core_strategy, **parse_params(args.core_param))
    core_syms = args.core_symbols or args.symbols
    if args.core_top:
        from .universe import kospi_top_symbols
        core_syms, names = kospi_top_symbols(args.core_top)
        print(f"장기 후보: 오늘 기준 코스피 시총 상위 {args.core_top} — {', '.join(names.values())}")
        print("(과거 시점의 상위 종목이 아니라 지금 상위 종목으로 계산하므로 결과가 실제보다 좋게 나올 수 있음)\n")
    data = load_data(args, list(dict.fromkeys(core_syms + args.symbols)))
    w = args.core_weight
    kw = dict(risk=risk, commission=cfg.commission, sell_tax=cfg.sell_tax, slippage=args.slippage)
    parts = [("장기", core, core_syms, w), ("적극", strategy, args.symbols, 1 - w)]
    results = []
    for name, strat, syms, weight in parts:
        res = run_backtest({s: data[s] for s in syms}, strat, cash=args.cash * weight, **kw)
        results.append((name, strat, weight, res))
    index = results[0][3].equity.index.union(results[1][3].equity.index)
    equity = sum(r.equity.reindex(index).ffill().bfill() for *_, r in results)
    combined = BacktestResult(equity=equity, trades=[t for *_, r in results for t in r.trades],
                              benchmark=_benchmark(data, index, equity.iloc[0]))
    print("== 합계")
    print_stats(combined.stats())
    print("\n== 전략별")
    for name, strat, weight, res in results:
        st = res.stats()
        print(f"[{name}] {strat.name} {weight:.0%}: 수익률 {st['총수익률']:.1%} · MDD {st['최대낙폭(MDD)']:.1%}"
              f" · 거래 {st['거래횟수']}")
    return 0


def cmd_backtest(args, strategy, risk, cfg) -> int:
    if args.core_strategy and not args.compare:
        return cmd_backtest_sleeves(args, strategy, risk, cfg)
    data = load_data(args, args.symbols)
    has_flow = all("foreign" in df.columns for df in data.values())

    def run(strat):
        return run_backtest(data, strat, cash=args.cash, risk=risk,
                            commission=cfg.commission, sell_tax=cfg.sell_tax,
                            slippage=args.slippage).stats()

    if args.compare:
        rows = []
        for name in STRATEGIES:
            strat = make_strategy(name)
            if strat.needs_flow and not has_flow:
                continue
            rows.append((name, run(strat)))
        first = rows[0][1]
        print(f"기간 {first['기간']} · 종목 {', '.join(data)}\n")
        labels = {"dual_momentum": "dual_mom"}
        print(f"{'전략':<9}{'수익률':>8}{'MDD':>8}{'거래':>6}{'승률':>6}")
        for name, st in rows:
            print(f"{labels.get(name, name):<10}{st['총수익률']:>9.1%}{st['최대낙폭(MDD)']:>8.1%}"
                  f"{st['거래횟수']:>6}{st['승률']:>6.0%}")
        print(f"{'단순보유':<8}{first['단순보유수익률']:>9.1%}{first['단순보유MDD']:>8.1%}")
        return 0

    if strategy.needs_flow and not has_flow:
        raise ValueError(f"{strategy.name} 전략은 투자자 수급 데이터가 필요합니다 (--fetch 또는 --demo)")
    print_stats(run(strategy))
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
    if args.command == "export-state":
        from .migrate import export_text
        print(export_text(sorted(Path(".").glob("bot_state_*.json"))))
        return 0
    if args.command == "import-state":
        from .migrate import import_text
        text = args.text or input("텔레그램 /export 로 받은 TBSTATE:... 한 줄을 붙여넣고 Enter: ")
        for name in import_text(text):
            print(f"가져옴: {name} (기존 파일은 .bak 으로 보관)")
        print("⚠️ 이전 기기의 봇이 꺼져 있는지 확인한 뒤 이 기기에서 봇을 시작하세요.")
        return 0
    if args.command == "stocks":
        from .universe import cheap_large_caps
        rows = cheap_large_caps(args.max_price, args.top)
        print(f"코스피 시총 상위 {args.top} 중 1주 {args.max_price:,.0f}원 이하 (시총 순, 전일 종가 기준)\n")
        for i, x in enumerate(rows, 1):
            print(f"{i:>2}. {x.code} {x.name}  {x.prev_close:,.0f}원  시총 {x.cap / 10000:,.1f}조")
        if not rows:
            print("조건에 맞는 종목이 없습니다. --max-price 를 올려보세요.")
        return 0
    if args.command == "setup":
        from .setup_wizard import Wizard
        return Wizard().run()
    if args.command == "telegram-setup":
        return cmd_telegram_setup()

    strategy = make_strategy(args.strategy, **parse_params(args.param))
    risk = RiskConfig(max_position_pct=args.max_position, stop_loss_pct=args.stop_loss,
                      take_profit_pct=args.take_profit, max_daily_loss_pct=args.max_daily_loss)
    cfg = BotConfig(symbols=args.symbols, strategy=args.strategy, risk=risk, sell_tax=args.sell_tax)

    if args.command == "backtest":
        return cmd_backtest(args, strategy, risk, cfg)

    # run
    if getattr(strategy, "intraday", False):
        raise ValueError(f"{strategy.name} 은 아직 백테스트 전용입니다 (실매매 미지원)")
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
    plan = strategy
    if args.core_strategy:
        from .bot import Sleeve
        core = make_strategy(args.core_strategy, **parse_params(args.core_param))
        if getattr(core, "intraday", False):
            raise ValueError(f"{core.name} 은 아직 백테스트 전용입니다 (실매매 미지원)")
        plan = [Sleeve(core, args.core_symbols or args.symbols, args.core_weight, "장기",
                       universe_top=args.core_top),
                Sleeve(strategy, args.symbols, 1 - args.core_weight, "적극")]
    watcher = None
    if args.tick > 0:
        from .ticks import TickWatcher
        traded = [s for sl in (plan if isinstance(plan, list) else []) for s in sl.symbols] or args.symbols
        watcher = TickWatcher(api, [*traded, *args.watch], vol_ratio=args.spike_vol, min_change=args.spike_pct)
    scalp_cfg = None
    if args.scalp_budget > 0:
        if not watcher:
            raise ValueError("--scalp-budget 은 --tick (장중 시세 감시) 과 함께 써야 합니다 (예: --tick 60)")
        from .scalp import ScalpConfig
        ch, cm = map(int, args.scalp_close.split(":"))
        scalp_cfg = ScalpConfig(budget_pct=args.scalp_budget, take_profit=args.scalp_take, stop_loss=args.scalp_stop,
                                trail=args.scalp_trail, max_trades_per_day=args.scalp_max_trades,
                                max_daily_loss_pct=args.scalp_max_loss, close_time=dtime(ch, cm))
    bot = TradingBot(broker, plan, cfg, state_path=state_path, trade_time=dtime(hh, mm),
                     notifier=make_notifier(), label=label, watcher=watcher, tick_sec=args.tick,
                     scalp_cfg=scalp_cfg)
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
