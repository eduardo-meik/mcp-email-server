#!/bin/sh
set -e

mkdir -p /run/clamav
chown clamav:clamav /run/clamav

clamd --foreground &
CLAMD_PID=$!
READY=0

for _ in $(seq 1 90); do
    if printf 'PING\n' | nc -w 1 127.0.0.1 3310 2>/dev/null | grep -q PONG; then
        READY=1
        break
    fi
    if ! kill -0 "$CLAMD_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "clamd failed to become ready" >&2
    exit 1
fi

uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}" &
UVICORN_PID=$!

trap 'kill $UVICORN_PID $CLAMD_PID 2>/dev/null' TERM INT
wait $UVICORN_PID
