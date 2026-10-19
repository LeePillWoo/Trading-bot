# 휴대폰으로 설치하기

자동매매는 **국내 클라우드 서버**에서 24시간 돌아가고, 휴대폰은 **텔레그램**으로 확인·제어만 합니다.

```
[클라우드 서버 (서울)]  ──주문/조회──▶  [나무증권 API]
        │
        └──알림/명령──▶  [텔레그램]  ◀──  📱 휴대폰
```

준비물 4가지: ① 나무증권 앱키/시크릿 ② 텔레그램 봇 토큰 ③ GitHub 토큰 ④ 클라우드 서버

> ⚠️ 키·토큰은 남에게 보여주거나 채팅/메신저에 붙여넣지 마세요. 서버의 `.env` 파일에만 넣습니다.

---

## 1. 나무증권 앱키 발급

[www.nhplug.com](https://www.nhplug.com) → **신청하기** → **namuh PLUG** (QR 로그인) → 앱키/시크릿을 메모장에 복사해 둡니다.
모의투자 이용 신청(교육 이수 등)이 있으면 함께 진행하세요. 처음에는 반드시 **모의투자**로 돌립니다.

## 2. 텔레그램 봇 만들기 (5분)

1. 텔레그램 앱에서 **@BotFather** 검색 → 대화 시작
2. `/newbot` 입력 → 봇 이름 입력 (예: 내 자동매매) → 아이디 입력 (`bot`으로 끝나야 함, 예: `myname_trade_bot`)
3. 받은 **토큰**(`123456:ABC...` 형태)을 복사해 둡니다
4. 방금 만든 봇을 검색해서 **시작(Start)** 을 눌러 둡니다

## 3. GitHub 토큰 만들기 (저장소가 비공개라 필요)

1. github.com 로그인 → 오른쪽 위 프로필 → **Settings** → 맨 아래 **Developer settings**
2. **Personal access tokens → Fine-grained tokens → Generate new token**
3. Repository access: **Only select repositories → Trading-bot**
4. Permissions → Repository permissions → **Contents: Read-only**
5. 생성된 토큰(`github_pat_...`)을 복사해 둡니다

## 4. 클라우드 서버 만들기 (AWS Lightsail 서울)

Lightsail은 **브라우저에서 바로 서버 터미널을 열 수 있어** 휴대폰으로 관리하기 편합니다.

1. [lightsail.aws.amazon.com](https://lightsail.aws.amazon.com) 가입/로그인 (결제 카드 필요)
2. **Create instance**
   - Region: **Seoul (ap-northeast-2)**
   - Platform: **Linux/Unix** → Blueprint: **OS Only → Ubuntu 24.04 LTS**
   - Plan: **공인 IPv4가 포함된** 가장 작은 요금제 (요금은 가입 화면에서 확인)
3. **Create instance** → 1~2분 뒤 상태가 Running 이 되면, 인스턴스의 **터미널 아이콘(Connect using SSH)** 을 누릅니다

> Oracle Cloud(서울/춘천)의 무료 서버를 써도 됩니다. 아래 명령은 Ubuntu면 동일합니다.

## 5. 서버에 봇 설치

열린 터미널에 아래를 **한 줄씩** 붙여넣습니다.

```bash
sudo apt-get update -y && sudo apt-get install -y git
git clone -b ccr-b7de564b-nfauhm https://github.com/LeePillWoo/Trading-bot.git
```
- Username: `LeePillWoo`
- Password: 3번에서 만든 **GitHub 토큰** (입력해도 화면에 안 보이는 게 정상)

```bash
cd Trading-bot
bash deploy/install.sh
```

## 6. 키 입력

```bash
nano .env
```
화살표로 이동해서 `=` 뒤에 값을 붙여넣습니다 (띄어쓰기·따옴표 없이).

```
NHPLUG_APP_KEY=앱키
NHPLUG_APP_SECRET=시크릿
NHPLUG_MODE=paper
TELEGRAM_BOT_TOKEN=텔레그램_봇_토큰
```
저장: **Ctrl+O → Enter → Ctrl+X** (휴대폰 키보드에 Ctrl이 없으면 터미널 화면의 Ctrl 버튼 사용)

## 7. 연결 확인

```bash
.venv/bin/python -m trading_bot telegram-setup
```
→ 텔레그램에서 내 봇에게 아무 말이나 보내면 `chat_id`가 나옵니다. `nano .env`로 `TELEGRAM_CHAT_ID=` 뒤에 넣으세요.
텔레그램에 "✅ 연결 확인 완료"가 오면 성공입니다.

```bash
.venv/bin/python -m trading_bot accounts
```
→ **모의투자(03) ✅ 사용 가능** 으로 표시된 계좌번호를 `.env`의 `NHPLUG_ACCOUNT=` 뒤에 넣으세요.

```bash
.venv/bin/python -m trading_bot backtest --fetch --symbols 005930 000660
```
→ 실제 일봉으로 백테스트가 돌면 시세 연동도 정상입니다.

## 8. 시작

```bash
sudo systemctl start trading-bot
```
텔레그램으로 **🤖 자동매매 봇 시작** 메시지가 오면 끝입니다. 이제 서버 창은 닫아도 됩니다 (서버가 재부팅돼도 자동으로 다시 켜집니다).

---

## 휴대폰에서 매일 보는 것

| 언제 | 텔레그램 메시지 |
|---|---|
| 09:05 | ☀️ 오늘의 매매 완료 — 신호, 주문, 잔고 |
| 매수/매도/손절 시 | 🔴 매수 / 🔵 매도 주문 알림 |
| 15:35 | 🌙 장 마감 보고 — 총자산, 오늘 손익, 보유 종목 |
| 문제 발생 | ⚠️ 오류 / ⛔ 일시정지 |

**명령** (봇에게 입력)

| 명령 | 기능 |
|---|---|
| `/status` | 지금 상태와 잔고 |
| `/stop` | **비상정지** — 모든 자동 주문 중단 (보유 종목은 그대로, 필요하면 나무 앱에서 직접 매도) |
| `/start` | 매매 재개 |

**자동 일시정지** — 아래 상황에서는 봇이 스스로 멈추고 알려줍니다. 확인 후 `/start`로 재개하세요.
- 하루 손실이 장 시작 대비 3%를 넘을 때
- 하루 주문이 20회를 넘을 때

> 09:05 보고나 15:35 보고가 **오지 않으면** 봇이나 서버가 멈춘 것입니다. Lightsail에서 터미널을 열고 `sudo systemctl status trading-bot` 으로 확인하세요.

## 설정 바꾸기

`nano .env` 에서 `BOT_ARGS` 줄을 고친 뒤 `sudo systemctl restart trading-bot`.

```
BOT_ARGS=--symbols 005930 000660 035420 --strategy sma --param short=10 long=60 --stop-loss 0.05
```

## 실전 전환 (모의투자로 충분히 확인한 뒤에만)

1. `.env`: `NHPLUG_MODE=real`, `NHPLUG_ACCOUNT=실전계좌(01)`, `BOT_ARGS` 끝에 ` --confirm-real` 추가
2. `sudo systemctl restart trading-bot`
3. 텔레그램 메시지 앞에 **[실전]** 이 붙는지 확인

## 코드 업데이트

```bash
cd ~/Trading-bot && bash deploy/update.sh
```

## 참고

- 나무증권 접근토큰은 1시간마다 갱신되며, 갱신될 때마다 나무증권 보안 알림이 올 수 있습니다 (정상).
- 오늘 일봉이 없으면 휴장일로 보고 매매하지 않습니다.
- 로그: `journalctl -u trading-bot -f` 또는 `~/Trading-bot/logs/trading.log`
