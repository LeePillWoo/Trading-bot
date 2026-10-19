#!/data/data/com.termux/files/usr/bin/bash
# 봇 중지
pkill -f "while true; do python -m trading_bot service" || true
pkill -f "trading_bot service" || true
termux-wake-unlock 2>/dev/null || true
echo "봇을 중지했습니다."
