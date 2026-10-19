#!/usr/bin/env bash
# 한 줄 설치: 코드 받기 → 설치 → 설정 마법사 → 봇 시작
#   bash <(curl -fsSL https://raw.githubusercontent.com/LeePillWoo/Trading-bot/ccr-b7de564b-nfauhm/deploy/bootstrap.sh)
# 다시 실행하면 최신 코드로 업데이트하고 설정을 확인한 뒤 재시작한다.
set -euo pipefail

REPO="https://github.com/LeePillWoo/Trading-bot.git"
BRANCH="${BRANCH:-ccr-b7de564b-nfauhm}"
DIR="$HOME/Trading-bot"

sudo apt-get update -y -q
sudo apt-get install -y -q git

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

bash deploy/install.sh
.venv/bin/python -m trading_bot setup
sudo systemctl restart trading-bot

echo
echo "🚀 봇을 시작했습니다. 텔레그램으로 시작 메시지가 오면 끝입니다. 이 창은 닫아도 됩니다."
