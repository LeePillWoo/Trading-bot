# 내 휴대폰(Termux)에서 실행하기

## 설치 (한 번만)

Termux 화면에 아래 한 줄을 붙여넣고 Enter:

```bash
pkg update -y && pkg install -y git && (git -C ~/Trading-bot pull || git clone -b ccr-b7de564b-nfauhm https://github.com/LeePillWoo/Trading-bot.git ~/Trading-bot) && bash ~/Trading-bot/deploy/termux.sh
```

- 중간에 `[Y/n]` 같은 질문이 나오면 **Enter**
- 패키지 설치가 끝나면 **설정 마법사**가 나옵니다 (앱키, 시크릿, 텔레그램 토큰 붙여넣기 → 나머지는 Enter)
- 텔레그램으로 **🤖 자동매매 봇 시작** 이 오면 완료

설정 변경·업데이트도 **같은 한 줄을 다시 실행**하면 됩니다.

## 꼭 해야 하는 휴대폰 설정 (안 하면 봇이 꺼집니다)

1. **배터리 절전 예외**: 설정 → 애플리케이션 → Termux → 배터리 → **제한 없음**
2. **Wake lock**: 알림창의 Termux 알림에 `wake lock held` 가 보이는지 확인 (없으면 알림의 **Acquire wakelock** 터치)
3. Termux 는 `exit` 로 닫지 말고 **홈 버튼으로 나가기**. 최근 앱 "모두 닫기" 도 주의
4. (선택) **Termux:Boot** 앱(F-Droid) 설치 후 한 번 실행 → 폰 재부팅 시 봇 자동 시작

## 명령어

| 하고 싶은 것 | 명령 |
|---|---|
| 봇 켜기 | `bash ~/Trading-bot/deploy/termux-start.sh` |
| 봇 끄기 | `bash ~/Trading-bot/deploy/termux-stop.sh` |
| 전략 비교 백테스트 (실제 과거 데이터) | `cd ~/Trading-bot && python -m trading_bot backtest --fetch --compare --symbols 005930 000660` |
| 로그 보기 | `tail -f ~/Trading-bot/logs/trading.log` (끝내기: Ctrl+C) |

평소 확인·제어는 텔레그램 `/status` `/report` `/stop` `/start` 로 하면 됩니다 (`/rebalance` 오늘 매매 재실행, `/export` 기기 이사). 09:05·15:35 보고가 안 오면 봇이 꺼진 것입니다.

## 권장: 손절은 증권사 감시주문으로도 걸어두기

휴대폰은 서버보다 꺼질 일이 많습니다. 나무증권 앱의 **감시주문(스탑로스)** 을 보유 종목에 걸어두면, 봇이 꺼져 있어도 증권사 서버가 손절을 처리합니다.
