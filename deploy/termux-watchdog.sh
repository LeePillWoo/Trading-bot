#!/data/data/com.termux/files/usr/bin/bash
# 감시 작업 등록: 안드로이드가 Termux(봇)를 죽여도 15분 안에 자동으로 다시 켠다.
#
# 안드로이드 JobScheduler 에 "15분마다 termux-start.sh 실행"을 등록한다. 앱이 죽어 있어도 시스템이
# Termux 를 깨워 스크립트를 돌리므로, 봇이 꺼져 있으면 다시 켜지고 켜져 있으면 아무것도 안 한다.
# 재부팅 뒤에도 유지된다(--persisted). Termux:API 앱이 설치돼 있어야 한다 (Termux 를 받은 곳과 같은 곳에서).
#
#   등록:  bash deploy/termux-watchdog.sh
#   확인:  termux-job-scheduler --pending
#   해제:  bash deploy/termux-watchdog.sh off
set -euo pipefail
cd "$(dirname "$0")/.."
JOB_ID=7

if [ "${1:-}" = "off" ]; then
  termux-job-scheduler --cancel --job-id $JOB_ID 2>/dev/null || true
  echo "감시 작업을 해제했습니다."
  exit 0
fi

if ! command -v termux-job-scheduler >/dev/null 2>&1; then
  echo "== termux-api 패키지 설치"
  pkg install -y termux-api
fi
if ! timeout 15 termux-job-scheduler --pending >/dev/null 2>&1; then
  cat <<MSG
❌ Termux:API 앱이 없거나 응답하지 않습니다.
   Termux 를 받은 곳(F-Droid 또는 GitHub)과 같은 곳에서 'Termux:API' 앱을 설치하고 한 번 실행한 뒤
   이 명령을 다시 실행하세요.  (F-Droid: https://f-droid.org/packages/com.termux.api/)
MSG
  exit 1
fi

termux-job-scheduler --job-id $JOB_ID --script "$PWD/deploy/termux-start.sh" \
  --period-ms 900000 --persisted true --battery-not-low false
echo
termux-job-scheduler --pending
echo
echo "✅ 감시 등록 완료: 봇이 꺼져 있으면 15분 안에 자동으로 다시 켭니다 (재부팅 뒤에도 유지)."
echo "   다시 켜질 때마다 텔레그램에 '🤖 자동매매 봇 시작' 이 옵니다."
