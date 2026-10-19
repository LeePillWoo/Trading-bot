#!/usr/bin/env bash
# 최신 코드로 업데이트하고 봇을 재시작한다.  사용: bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
.venv/bin/pip install -r requirements.txt -q
sudo systemctl restart trading-bot
echo "업데이트 완료. 텔레그램으로 시작 알림이 옵니다."
