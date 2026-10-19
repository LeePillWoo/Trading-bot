#!/usr/bin/env bash
# 서버(Ubuntu)에 자동매매 봇을 설치하고 부팅 시 자동 실행되도록 등록한다.
# 사용: 저장소 폴더 안에서  bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

echo "== 패키지 설치"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

echo "== 파이썬 가상환경"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  cp .env.example .env
  echo "== .env 파일을 만들었습니다. 'nano .env' 로 키를 입력하세요."
fi
chmod 600 .env

echo "== 서비스 등록 (trading-bot)"
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$APP_DIR|g" deploy/trading-bot.service \
  | sudo tee /etc/systemd/system/trading-bot.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable trading-bot

cat <<MSG

설치 완료. 다음 순서로 진행하세요.
  1) nano .env                                  # 키 입력 후 Ctrl+O, Enter, Ctrl+X
  2) .venv/bin/python -m trading_bot telegram-setup   # 텔레그램 chat_id 확인
  3) .venv/bin/python -m trading_bot accounts         # 나무증권 계좌번호 확인
  4) sudo systemctl start trading-bot           # 봇 시작 (텔레그램으로 시작 알림이 옵니다)

상태 확인: sudo systemctl status trading-bot
로그 보기: journalctl -u trading-bot -f
MSG
