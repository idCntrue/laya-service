#!/usr/bin/env bash
#
# Start the Laya service in the background.
#
# Writes the PID to .run/laya.pid and all output to logs/laya.log. Idempotent:
# running it twice will not start a second instance.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PID_FILE=".run/laya.pid"
LOG_FILE="logs/laya.log"
VENV_PY=".venv/bin/python"

mkdir -p .run logs

# --- Guard against a double start -------------------------------------------
if [[ -f "${PID_FILE}" ]]; then
    existing_pid="$(cat "${PID_FILE}")"
    if kill -0 "${existing_pid}" 2>/dev/null; then
        echo "laya-service is already running (pid ${existing_pid})"
        echo "use 'make stop' first, or 'make restart'"
        exit 0
    fi
    echo "removing stale pid file (pid ${existing_pid} is gone)"
    rm -f "${PID_FILE}"
fi

# --- Environment ------------------------------------------------------------
if [[ ! -f .env ]]; then
    echo "no .env found; creating one from .env.example"
    cp .env.example .env
fi

# Load .env so that HOST/PORT are available to this script. Values are NOT
# exported into the child's environment from here -- pydantic-settings reads
# .env itself, which keeps one source of truth.
set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ ! -x "${VENV_PY}" ]]; then
    echo "error: ${VENV_PY} not found. Run 'make install-dev' first." >&2
    exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9800}"

# --- Start ------------------------------------------------------------------
echo "starting laya-service on ${HOST}:${PORT} ..."
nohup "${VENV_PY}" -m laya_service.main >> "${LOG_FILE}" 2>&1 &
pid=$!
echo "${pid}" > "${PID_FILE}"

# --- Wait for readiness -----------------------------------------------------
# Poll /healthz (liveness) rather than /readyz, because the model may still be
# loading on a cold start and that is not a startup failure.
for _ in $(seq 1 30); do
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "error: process exited during startup. Last log lines:" >&2
        tail -n 20 "${LOG_FILE}" >&2
        rm -f "${PID_FILE}"
        exit 1
    fi
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
        echo "laya-service is up (pid ${pid})"
        echo "  log:      ${LOG_FILE}"
        echo "  liveness: http://127.0.0.1:${PORT}/healthz"
        echo "  docs:     http://127.0.0.1:${PORT}/docs"
        exit 0
    fi
    sleep 1
done

echo "warning: /healthz did not respond within 30s (pid ${pid} is still alive)"
echo "check the log: tail -f ${LOG_FILE}"
exit 0
