#!/bin/bash
# 무인 실행: 잠자기 방지(caffeinate) + 터미널 닫아도 유지(nohup) + 로그 파일
#   ./start.sh        드라이런 (실주문 없음)
#   ./start.sh live   ⚠️ 실전 소액 (.env에 LIVE_TRADING=1 필요)
cd "$(dirname "$0")"
if [ -f logs/watch.pid ] && kill -0 "$(cat logs/watch.pid)" 2>/dev/null; then
    echo "이미 실행 중입니다 (PID $(cat logs/watch.pid)). 먼저 ./stop.sh 하세요."
    exit 1
fi
mkdir -p logs
# 로그 회전: 5MB 넘으면 이전 로그 보관 (감사: 무한 증가 방지)
if [ -f logs/watch.log ] && [ "$(stat -f%z logs/watch.log)" -gt 5242880 ]; then
    mv logs/watch.log "logs/watch.$(date +%Y%m%d%H%M).log"
fi
FLAGS="--watch"
MODE="드라이런"
if [ "$1" = "live" ]; then
    FLAGS="--watch --live"
    MODE="⚠️ 실전"
fi
nohup caffeinate -i python3 -u run_dryrun.py $FLAGS >> logs/watch.log 2>&1 &
echo $! > logs/watch.pid
sleep 3
if ! kill -0 "$(cat logs/watch.pid)" 2>/dev/null; then
    echo "❌ 시작 실패 — 로그 확인:"; tail -5 logs/watch.log; rm -f logs/watch.pid; exit 1
fi
echo "✅ $MODE 모드 백그라운드 시작 (PID $(cat logs/watch.pid))"
echo "   로그 보기:   tail -f logs/watch.log"
echo "   중지:        ./stop.sh   (폰에서는 텔레그램 /stop /flat)"
echo "   ⚠️ 뚜껑 닫으려면: sudo pmset -a disablesleep 1 (돌아와서 0으로 원복). 전원 연결 권장."
