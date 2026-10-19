#!/data/data/com.termux/files/usr/bin/bash
# 봇을 백그라운드로 시작 (이미 실행 중이면 그대로 둠). 죽으면 30초 뒤 자동 재시작.
cd "$(dirname "$0")/.."
if pgrep -f "trading_bot service" >/dev/null; then
  echo "이미 실행 중입니다."
  exit 0
fi
termux-wake-lock 2>/dev/null || true
mkdir -p logs
nohup bash -c 'while true; do python -m trading_bot service; echo "재시작 대기 30초"; sleep 30; done' \
  >> logs/service.log 2>&1 &
sleep 3
if pgrep -f "trading_bot service" >/dev/null; then
  echo "봇 시작됨."
else
  echo "시작 실패. 로그 확인: tail -n 30 logs/service.log"
  exit 1
fi
