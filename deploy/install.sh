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
  echo "== .env 파일을 만들었습니다."
fi
chmod 600 .env

echo "== 서비스 등록 (trading-bot)"
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$APP_DIR|g" deploy/trading-bot.service \
  | sudo tee /etc/systemd/system/trading-bot.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable trading-bot

echo "== 설치 완료 (설정: .venv/bin/python -m trading_bot setup)"
