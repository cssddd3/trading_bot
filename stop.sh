#!/bin/bash
cd "$(dirname "$0")"
if [ -f logs/watch.pid ] && kill -0 "$(cat logs/watch.pid)" 2>/dev/null; then
    kill "$(cat logs/watch.pid)" && rm -f logs/watch.pid
    echo "중지했습니다."
else
    rm -f logs/watch.pid
    echo "실행 중인 봇이 없습니다."
fi
