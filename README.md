# Trading-bot

**나무증권(NH투자증권) Open API(NHPLUG)** 기반 국내주식 자동매매 봇입니다. 한국투자증권(KIS) API도 `--api kis`로 쓸 수 있습니다.
전략 백테스트 → 로컬 가상매매 → 모의투자 → 실전 순서로 단계적으로 검증할 수 있도록 구성되어 있습니다.

**👉 설치: 내 휴대폰에서 실행 → [TERMUX.md](TERMUX.md) · 클라우드 서버에서 실행 → [SETUP.md](SETUP.md)**

구조: 국내 클라우드 서버에서 봇이 24시간 실행되고, 휴대폰은 텔레그램으로 알림을 받고 `/status` `/stop` `/start`로 제어합니다.

> ⚠️ 투자 손실에 대한 책임은 사용자에게 있습니다. 반드시 백테스트와 모의투자로 충분히 검증한 뒤 소액으로 시작하세요.

## 구성

```
trading_bot/
├── strategy.py     # 매매 전략 (sma, rsi, breakout)
├── risk.py         # 포지션 사이징, 손절/익절
├── backtest.py     # 일봉 백테스트 (다음날 시가 체결, 수수료/세금 반영)
├── bot.py          # 실시간 자동매매 루프
├── data.py         # CSV 로딩, 데모용 데이터
├── notify.py       # 텔레그램 알림/명령
├── config.py       # 환경변수/설정
└── broker/
    ├── base.py     # 브로커 인터페이스
    ├── paper.py    # 로컬 가상매매 브로커
    ├── namuh.py    # 나무증권(NH투자증권) NHPLUG REST API (기본)
    └── kis.py      # 한국투자증권 REST API
```

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # 앱키/시크릿/계좌번호 입력
```

### 나무증권 설정

1. [nhplug.com](https://www.nhplug.com)에서 Open API를 신청하고 앱키/시크릿을 발급받아 `.env`의 `NHPLUG_APP_KEY`, `NHPLUG_APP_SECRET`에 넣습니다.
2. 계좌번호 확인:
   ```bash
   python -m trading_bot accounts
   ```
   `acct_type` 03 = 모의투자 계좌, 01·02 = 실전 계좌입니다. 모드(`NHPLUG_MODE`)에 맞는 계좌를 `NHPLUG_ACCOUNT`에 넣습니다.
3. `NHPLUG_MODE=paper`(모의투자, 기본)로 먼저 검증하고, 충분히 확인한 뒤에만 `real`로 바꿉니다.

참고 사항 (공식 명세 기준):
- 접근토큰은 실전 서버에서만 발급되며 24시간 유효합니다. **재발급할 때마다 보안 알림이 오므로** `.nhplug_token.json`에 캐시해 재사용합니다.
- 시세 API(일봉, 현재가)는 모의투자 서버에서 제공되지 않아, 모의투자 모드에서도 **시세는 실전 서버에서 조회**하고 주문·잔고만 모의투자 서버로 보냅니다.
- 주문 전에 매수/매도 가능수량을 조회해 그 이하로만 주문합니다 (미수 방지).
- 호출 한도(초당 약 5회)에 맞춰 초당 4회로 제한합니다.
- N2 고객은 `NHPLUG_DOMAIN=n2plug.com`.

### 한국투자증권 (선택)

[KIS Developers](https://apiportal.koreainvestment.com)에서 앱키를 발급받아 `KIS_*` 값을 넣고, 명령에 `--api kis`를 붙입니다.

## 1. 백테스트

```bash
# 데모 데이터
python -m trading_bot backtest --demo --symbols 005930 000660

# CSV (date,open,high,low,close,volume 또는 날짜,시가,고가,저가,종가,거래량 / 파일명 = 종목코드)
python -m trading_bot backtest --csv data/005930.csv --strategy rsi

# 나무증권 API로 일봉 조회 (--api kis 로 한국투자증권)
python -m trading_bot backtest --fetch --symbols 005930 000660 --days 500 \
    --strategy sma --param short=10 long=60 --stop-loss 0.05
```

출력: 총수익률, CAGR, MDD, 샤프지수, 거래횟수, 승률, 단순보유 대비 수익률.

전략 비교 (같은 데이터로 모든 전략을 한 표로):
```bash
python -m trading_bot backtest --fetch --compare --symbols 005930 000660
```

## 2. 자동매매 실행

```bash
# 로컬 가상매매: 실시세로 신호를 계산하지만 주문은 메모리에서만 시뮬레이션
python -m trading_bot run --symbols 005930 000660

# 나무증권 모의투자 계좌로 주문 (NHPLUG_MODE=paper)
python -m trading_bot run --broker api --symbols 005930 000660

# 실전 주문 (NHPLUG_MODE=real + --confirm-real 필수)
python -m trading_bot run --broker api --symbols 005930 --confirm-real

# cron 등으로 한 번만 실행
python -m trading_bot run --broker api --once
```

### 매매 규칙

- 매 거래일 `--trade-time`(기본 09:05 KST)에 **전일까지 확정된 일봉**으로 신호를 계산하고 시장가 주문 → 백테스트(다음날 시가 체결)와 같은 규칙
- 장중 `--interval`(기본 300초)마다 보유 종목의 손절/익절 점검
- 손절/익절 후에는 신호가 한 번 0(매도)이 될 때까지 재진입하지 않음
- 종목당 최대 비중 `--max-position`(기본 20%)
- 안전장치: 하루 손실 `--max-daily-loss`(기본 3%) 또는 하루 주문 20회 도달 시 자동 일시정지, 텔레그램 `/stop` 비상정지 (일시정지 중에는 손절 포함 모든 주문 중단)
- 오늘 일봉이 없으면 휴장일로 판단해 매매하지 않음
- `--symbols`에 없는 보유 종목은 건드리지 않음
- 상태(당일 매매 여부, 재진입 금지 종목)는 `bot_state_*.json`에 저장되어 재시작해도 유지
- 로그: `logs/trading.log`

## 전략

| 이름 | 설명 | 파라미터 (기본값) |
|---|---|---|
| `sma_flow` (기본, 추천) | 상승 추세(단기 이평 > 장기 이평) **이고** 최근 N일 외국인+기관 순매수 합계 > 0 이면 매수, 추세 이탈 시 매도 (나무증권 수급 데이터 필요) | `short=5 long=20 flow_days=5` |
| `sma` | 단기 이평 > 장기 이평이면 보유 | `short=5 long=20` |
| `rsi` | RSI < 과매도 매수, RSI > 과매수 매도 | `period=14 oversold=30 overbought=70` |
| `breakout` | N일 고가 돌파 매수, M일 저가 이탈 매도 | `entry=20 exit=10` |
| `dual_momentum` | 매달 첫 거래일, 최근 lookback일 수익률 상위 top_n 종목 중 수익률이 무위험 수익률(rf)보다 높은 것만 보유 (없으면 현금). 후보 종목 5~15개 권장 | `lookback=252 top_n=2 rf=0.03` |
| `vb` (백테스트 전용) | 변동성 돌파: 오늘 시가 + 어제 범위×k 돌파 시 매수, 다음 날 시가 매도. 매일 매매해 거래세 부담이 큼 | `k=0.5 ma=0` |

새 전략은 `strategy.Strategy`를 상속해 `signals(df) -> Series[0/1]`을 구현하고 `STRATEGIES`에 등록하면 됩니다. t일 신호는 t일 종가까지의 데이터만 사용해야 합니다 (`tests/test_strategy_backtest.py::test_no_lookahead`가 검증).

## 장기 + 적극 나눠서 운용 (슬리브)

계좌를 비중별로 나눠 두 전략을 동시에 돌립니다. 각 전략은 자기가 산 수량을 장부에 따로 기록하고 자기 몫만 팝니다 (같은 종목을 둘 다 사도 섞이지 않음).

```bash
# 장기 70%: 대형주 12개 듀얼 모멘텀(상위 3개) / 적극 30%: breakout
python -m trading_bot run --broker api --strategy breakout --symbols 005930 000660 035420 \
    --core-strategy dual_momentum --core-param top_n=3 --core-weight 0.7 \
    --core-symbols 005930 000660 005380 000270 068270 035420 035720 051910 006400 105560 055550 012330

# 같은 구성 백테스트 (합계 + 전략별)
python -m trading_bot backtest --fetch --days 990 --slippage 0.001 --strategy breakout --symbols ... \
    --core-strategy dual_momentum --core-param top_n=3 --core-symbols ...
```

설정 마법사(`setup`)의 "운용 방식: 2"로도 설정할 수 있습니다.

## 비용 가정

수수료 0.015%, 매도 시 거래세 0.20% (`config.BotConfig`). 실제 증권사 수수료와 시장/연도별 세율에 맞게 수정하세요.

## 테스트

```bash
pip install pytest
python -m pytest
```

## 알려진 한계

- 체결 내역 API는 쓰지 않고, 주문 후 잔고로 확인해 텔레그램으로 보고
- 국내주식 현금 계좌, KRX 정규장만 지원 (해외주식, 신용, NXT/시간외, 분할매수 미지원)
- 나무증권 잔고는 연속조회를 하지 않음 (보유 종목이 한 페이지를 넘는 경우 일부 누락 가능)
- 나무증권 연동은 공식 명세(openapi.json, 260911)와 SDK 기준으로 작성했고, 실제 서버 연동은 사용자 앱키로 확인이 필요
