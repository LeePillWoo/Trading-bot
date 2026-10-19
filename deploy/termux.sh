#!/data/data/com.termux/files/usr/bin/bash
# Termux(안드로이드) 설치: 패키지 설치 → 설정 마법사 → 부팅 자동실행 등록 → 봇 시작
# 다시 실행하면 업데이트 후 재시작한다.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/4 패키지 설치 (처음엔 5~10분 걸립니다)"
yes | pkg upgrade -y || true
dpkg --configure -a || true          # 업그레이드 중 끊긴 설치 마무리
pkg install -y tur-repo              # pandas 가 있는 Termux 사용자 저장소
pkg update -y                        # 새 저장소 목록 받기
pkg install -y python python-numpy python-pip procps
has_pandas() { python -c "import pandas" 2>/dev/null; }
has_pandas || pkg install -y python-pandas || true
if ! has_pandas; then
  echo "   pandas 패키지가 없어 직접 빌드합니다 (10~30분 걸릴 수 있음)"
  pkg install -y clang make ninja binutils
  pip install pandas 2>/dev/null || pip install --break-system-packages pandas
fi
pip install -q requests tzdata 2>/dev/null || pip install -q --break-system-packages requests tzdata
python -c "import pandas, numpy, requests, zoneinfo; zoneinfo.ZoneInfo('Asia/Seoul'); print('   패키지 OK')"

echo "== 2/4 설정"
[ -f .env ] || cp .env.example .env
chmod 600 .env
python -m trading_bot setup

echo "== 3/4 휴대폰 재부팅 시 자동 실행 등록 (Termux:Boot 앱 필요)"
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-trading-bot <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
bash $PWD/deploy/termux-start.sh
BOOT
chmod +x ~/.termux/boot/start-trading-bot

echo "== 4/4 봇 시작"
bash deploy/termux-stop.sh >/dev/null 2>&1 || true
bash deploy/termux-start.sh

cat <<MSG

🚀 완료! 텔레그램으로 '자동매매 봇 시작' 메시지가 오면 정상입니다.
   - Termux 는 'exit' 로 닫지 말고 홈 버튼으로 나가세요.
   - 상단 알림의 Termux 에 'wake lock held' 가 보여야 합니다.
   - 끄기:    bash ~/Trading-bot/deploy/termux-stop.sh
   - 켜기:    bash ~/Trading-bot/deploy/termux-start.sh
   - 로그:    tail -f ~/Trading-bot/logs/trading.log
MSG
