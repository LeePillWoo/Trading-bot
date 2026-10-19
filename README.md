# Trading-bot

한국투자증권(KIS) Open API 기반 국내주식 자동매매 봇입니다.
전략 백테스트 → 로컬 가상매매 → KIS 모의투자 → 실전 순서로 단계적으로 검증할 수 있도록 구성되어 있습니다.

> ⚠️ 투자 손실에 대한 책임은 사용자에게 있습니다. 반드시 백테스트와 모의투자로 충분히 검증한 뒤 소액으로 시작하세요.

## 구성

```
trading_bot/
├── strategy.py     # 매매 전략 (sma, rsi, breakout)
├── risk.py         # 포지션 사이징, 손절/익절
├── backtest.py     # 일봉 백테스트 (다음날 시가 체결, 수수료/세금 반영)
├── bot.py          # 실시간 자동매매 루프
├── data.py         # CSV 로딩, 데모용 데이터
├── config.py       # 환경변수/설정
└── broker/
    ├── base.py     # 브로커 인터페이스
    ├── paper.py    # 로컬 가상매매 브로커
    └── kis.py      # 한국투자증권 REST API
```

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # 앱키/시크릿/계좌번호 입력
```

KIS 앱키는 [KIS Developers](https://apiportal.koreainvestment.com)에서 발급받습니다. 모의투자 계좌와 실전 계좌의 앱키는 서로 다르므로, `KIS_MODE` 값에 맞는 키를 넣어야 합니다.

## 1. 백테스트

```bash
# 데모 데이터
python -m trading_bot backtest --demo --symbols 005930 000660

# CSV (date,open,high,low,close,volume 또는 날짜,시가,고가,저가,종가,거래량 / 파일명 = 종목코드)
python -m trading_bot backtest --csv data/005930.csv --strategy rsi

# KIS API로 일봉 조회
python -m trading_bot backtest --kis --symbols 005930 000660 --days 500 \
    --strategy sma --param short=10 long=60 --stop-loss 0.05
```

출력: 총수익률, CAGR, MDD, 샤프지수, 거래횟수, 승률, 단순보유 대비 수익률.

## 2. 자동매매 실행

```bash
# 로컬 가상매매: KIS 시세로 신호를 계산하지만 주문은 메모리에서만 시뮬레이션
python -m trading_bot run --symbols 005930 000660

# KIS 모의투자 계좌로 주문 (KIS_MODE=paper)
python -m trading_bot run --broker kis --symbols 005930 000660

# 실전 주문 (KIS_MODE=real + --confirm-real 필수)
python -m trading_bot run --broker kis --symbols 005930 --confirm-real

# cron 등으로 한 번만 실행
python -m trading_bot run --broker kis --once
```

### 매매 규칙

- 매 거래일 `--trade-time`(기본 09:05 KST)에 **전일까지 확정된 일봉**으로 신호를 계산하고 시장가 주문 → 백테스트(다음날 시가 체결)와 같은 규칙
- 장중 `--interval`(기본 300초)마다 보유 종목의 손절/익절 점검
- 손절/익절 후에는 신호가 한 번 0(매도)이 될 때까지 재진입하지 않음
- 종목당 최대 비중 `--max-position`(기본 20%), 하루 최대 주문 20회
- `--symbols`에 없는 보유 종목은 건드리지 않음
- 상태(당일 매매 여부, 재진입 금지 종목)는 `bot_state_*.json`에 저장되어 재시작해도 유지
- 로그: `logs/trading.log`

## 전략

| 이름 | 설명 | 파라미터 (기본값) |
|---|---|---|
| `sma` | 단기 이평 > 장기 이평이면 보유 | `short=5 long=20` |
| `rsi` | RSI < 과매도 매수, RSI > 과매수 매도 | `period=14 oversold=30 overbought=70` |
| `breakout` | N일 고가 돌파 매수, M일 저가 이탈 매도 | `entry=20 exit=10` |

새 전략은 `strategy.Strategy`를 상속해 `signals(df) -> Series[0/1]`을 구현하고 `STRATEGIES`에 등록하면 됩니다. t일 신호는 t일 종가까지의 데이터만 사용해야 합니다 (`tests/test_strategy_backtest.py::test_no_lookahead`가 검증).

## 비용 가정

수수료 0.015%, 매도 시 거래세 0.20% (`config.BotConfig`). 실제 증권사 수수료와 시장/연도별 세율에 맞게 수정하세요.

## 테스트

```bash
pip install pytest
python -m pytest
```

## 알려진 한계

- 공휴일 달력은 없음 (휴장일에는 주문이 거부되고 로그에 남음)
- 시장가 주문 체결 여부는 별도로 조회하지 않음 (다음 점검 때 잔고로 반영)
- 국내주식 현금 계좌만 지원 (해외주식, 신용, 분할매수 미지원)
