#!/usr/bin/env bash
# 최신 코드로 업데이트하고 봇을 재시작한다.  사용: bash deploy/update.sh
# 리눅스 서버(systemd + .venv)와 휴대폰(Termux) 모두에서 동작한다.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
if [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -r requirements.txt -q
else
  pip install -q -r requirements.txt 2>/dev/null || pip install -q --break-system-packages -r requirements.txt
fi
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then
  bash deploy/termux-stop.sh
  bash deploy/termux-start.sh
else
  sudo systemctl restart trading-bot
fi
echo "업데이트 완료. 텔레그램으로 시작 알림이 옵니다."
