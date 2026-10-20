# 윈도우 컴퓨터에서 실행하기

> ⚠️ **같은 계좌로 휴대폰과 컴퓨터에서 동시에 돌리면 안 됩니다.** 주문이 두 번 나가고 장부가 엇갈립니다.
> 실수로 둘 다 켜지면 봇이 텔레그램 충돌을 감지해 **양쪽 모두 자동 일시정지**하고 알려줍니다.

## 1. 설치만 해두기 (봇은 켜지지 않음)

1. 파이썬이 없으면 [python.org](https://www.python.org/downloads/)에서 설치 — 첫 화면에서 **"Add python.exe to PATH" 체크**
2. 시작 메뉴 → **PowerShell** 열고 한 줄 실행:

```powershell
irm https://raw.githubusercontent.com/LeePillWoo/Trading-bot/ccr-b7de564b-nfauhm/deploy/windows/install.ps1 | iex
```

- 설치 위치: `C:\Users\<사용자>\Trading-bot`
- 마지막에 "설정 마법사를 실행할까요?" → `y` 면 휴대폰과 **같은 키·텔레그램 토큰·같은 모드(실전)** 로 설정
- 다시 실행하면 코드만 최신으로 바뀌고 `.env`·상태 파일은 그대로

설치만 한 상태에서도 백테스트는 바로 해볼 수 있습니다:

```powershell
cd $env:USERPROFILE\Trading-bot
.\.venv\Scripts\python -m trading_bot backtest --fetch --compare --days 990 --symbols 010140 024110 011200 032640
```

## 2. 휴대폰 → 컴퓨터로 옮길 때 (적용)

순서가 중요합니다.

1. **텔레그램에 `/export`** → "TBSTATE:..." 로 시작하는 메시지가 옴 (봇이 산 종목 장부. 키·토큰 없음)
2. **휴대폰 봇 끄기** (Termux):
   ```bash
   bash ~/Trading-bot/deploy/termux-stop.sh
   ```
   (재부팅 자동 시작도 막으려면 `rm ~/.termux/boot/start-trading-bot`)
3. **컴퓨터에 장부 가져오기** (PowerShell):
   ```powershell
   cd $env:USERPROFILE\Trading-bot
   .\.venv\Scripts\python -m trading_bot import-state
   ```
   → 1번에서 받은 `TBSTATE:...` 메시지 전체를 복사해 붙여넣고 Enter
4. **컴퓨터 봇 시작** (윈도우 로그인할 때마다 자동 시작도 등록됨):
   ```powershell
   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Trading-bot\deploy\windows\start.ps1"
   ```
5. 텔레그램에 `🤖 자동매매 봇 시작` 이 오면 끝. `/report` 로 장부(보유 종목)가 그대로인지 확인

> 3번을 빼먹으면 휴대폰 봇이 산 종목을 컴퓨터 봇이 모르고 **팔지 못합니다.**

## 3. 컴퓨터 설정 (장중에 꺼지지 않게)

- 봇이 도는 동안은 **절전 모드로 안 들어가게** 봇이 직접 막습니다 (화면은 꺼져도 됨)
- **윈도우 업데이트 재부팅**: 설정 → Windows 업데이트 → 고급 옵션 → **사용 시간**을 08:00~16:00 으로
- 노트북이면 **덮개를 닫아도 절전 안 함**: 제어판 → 전원 옵션 → 덮개를 닫으면 → "아무것도 안 함"
- 재부팅되면 로그인 후 자동으로 다시 시작됩니다 (로그인은 해야 함)

## 4. 다시 휴대폰으로 돌아갈 때

1. 컴퓨터에서 텔레그램 `/export` → 메시지 복사
2. 컴퓨터 봇 중지:
   ```powershell
   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Trading-bot\deploy\windows\stop.ps1"
   ```
3. 휴대폰 Termux: `cd ~/Trading-bot && python -m trading_bot import-state` 에 붙여넣기
4. `bash ~/Trading-bot/deploy/termux-start.sh`

## 명령 정리

| 하고 싶은 것 | PowerShell |
|---|---|
| 업데이트 | 1번의 설치 한 줄 다시 실행 → 봇 켜져 있으면 `stop.ps1` 후 `start.ps1` |
| 시작 (+자동 시작 등록) | `powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Trading-bot\deploy\windows\start.ps1"` |
| 중지 (+자동 시작 해제) | `powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Trading-bot\deploy\windows\stop.ps1"` |
| 로그 | `Get-Content $env:USERPROFILE\Trading-bot\logs\trading.log -Tail 30 -Wait` |
