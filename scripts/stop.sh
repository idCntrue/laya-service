#!/usr/bin/env bash
#
# Stop the background Laya service.
#
# Sends SIGTERM first so that Uvicorn can drain in-flight requests (the app
# configures a 30s graceful shutdown window), then escalates to SIGKILL only if
# the process is still alive after the timeout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PID_FILE=".run/laya.pid"
TIMEOUT="${STOP_TIMEOUT:-30}"

if [[ ! -f "${PID_FILE}" ]]; then
    echo "laya-service is not running (no pid file)"
    exit 0
fi

pid="$(cat "${PID_FILE}")"

if ! kill -0 "${pid}" 2>/dev/null; then
    echo "laya-service is not running (pid ${pid} is gone)"
    rm -f "${PID_FILE}"
    exit 0
fi

echo "stopping laya-service (pid ${pid}) ..."
kill -TERM "${pid}" 2>/dev/null || true

for _ in $(seq 1 "${TIMEOUT}"); do
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "stopped gracefully"
        rm -f "${PID_FILE}"
        exit 0
    fi
    sleep 1
done

echo "warning: still alive after ${TIMEOUT}s, sending SIGKILL"
kill -9 "${pid}" 2>/dev/null || true
sleep 1

if kill -0 "${pid}" 2>/dev/null; then
    echo "error: could not kill pid ${pid}" >&2
    exit 1
fi

echo "stopped (forced)"
rm -f "${PID_FILE}"
