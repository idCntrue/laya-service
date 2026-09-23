#!/usr/bin/env bash
#
# Report the state of the Laya service.
#
# The service can be started two ways, and this script detects which is in use
# rather than assuming:
#
#   * systemd  -- `systemctl --user start laya-service` (recommended; survives
#                 logout when linger is enabled, and restarts on crash)
#   * script   -- `make start`, which backgrounds a process and records its PID
#                 in .run/laya.pid
#
# Running both at once is a misconfiguration: the second one to start fails to
# bind the port. This script reports that case explicitly instead of silently
# showing one of them.
#
# Exits non-zero when the service is not reachable, so it works as a check.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PID_FILE=".run/laya.pid"
UNIT="laya-service"
PORT="${PORT:-9800}"
BASE="http://127.0.0.1:${PORT}"

status_code=0

# --- Detect systemd ---------------------------------------------------------
systemd_active=0
systemd_enabled=""
systemd_pid=""
systemd_restarts=""

if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user is-active "${UNIT}" >/dev/null 2>&1; then
        systemd_active=1
        systemd_pid="$(systemctl --user show "${UNIT}" -p ExecMainPID --value 2>/dev/null)"
        systemd_restarts="$(systemctl --user show "${UNIT}" -p NRestarts --value 2>/dev/null)"
        if systemctl --user is-enabled "${UNIT}" >/dev/null 2>&1; then
            systemd_enabled="yes"
        else
            systemd_enabled="no"
        fi
    fi
fi

# --- Detect script-managed process ------------------------------------------
script_pid=""
if [[ -s "${PID_FILE}" ]]; then
    candidate="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${candidate}" ]] && kill -0 "${candidate}" 2>/dev/null; then
        script_pid="${candidate}"
    fi
fi

# --- Report which mode is running -------------------------------------------
echo "== how it is running =="
if [[ "${systemd_active}" -eq 1 && -n "${script_pid}" ]]; then
    # Both alive: they cannot both own the port, so one is failing to bind.
    echo "  CONFLICT: both systemd and a script-managed process are alive"
    echo "    systemd  pid ${systemd_pid}"
    echo "    script   pid ${script_pid}  (from ${PID_FILE})"
    echo "  Only one can hold port ${PORT}. Stop one:"
    echo "    make stop                              # kills the script one"
    echo "    systemctl --user stop ${UNIT}   # stops the systemd one"
    status_code=1
elif [[ "${systemd_active}" -eq 1 ]]; then
    echo "  systemd (user unit '${UNIT}')"
elif [[ -n "${script_pid}" ]]; then
    echo "  script (pid ${script_pid}, from ${PID_FILE})"
else
    echo "  not running"
    if [[ -f "${PID_FILE}" ]]; then
        echo "  note: ${PID_FILE} exists but its process is gone (stale)"
    fi
    status_code=1
fi

# --- Process detail ---------------------------------------------------------
echo
echo "== process =="
shown_pid="${systemd_pid:-${script_pid}}"
if [[ -n "${shown_pid}" ]] && kill -0 "${shown_pid}" 2>/dev/null; then
    ps -o pid,etime,rss,pcpu,cmd -p "${shown_pid}" --no-headers 2>/dev/null \
        | awk '{printf "  pid=%s uptime=%s rss=%sKB cpu=%s%%\n  cmd=%s\n", $1, $2, $3, $4, substr($0, index($0,$5))}'
    if [[ "${systemd_active}" -eq 1 ]]; then
        echo "  autostart on login: ${systemd_enabled}"
        echo "  restarts by systemd: ${systemd_restarts}"
        # linger is what keeps the user manager (and thus this service) alive
        # after the last SSH session closes. Without it the service dies on
        # logout -- a common source of "why did it restart?" confusion.
        if command -v loginctl >/dev/null 2>&1; then
            linger="$(loginctl show-user "${USER}" -p Linger --value 2>/dev/null || echo unknown)"
            echo "  linger (survives logout): ${linger}"
            if [[ "${linger}" == "no" ]]; then
                echo "    ^ service will stop when your last session closes."
                echo "      fix: sudo loginctl enable-linger ${USER}"
            fi
        fi
    fi
else
    echo "  no live process"
fi

# --- Port -------------------------------------------------------------------
echo
echo "== port =="
if command -v ss >/dev/null 2>&1; then
    if ss -ltnp 2>/dev/null | grep -q ":${PORT} "; then
        ss -ltnp 2>/dev/null | grep ":${PORT} " | sed 's/^/  /'
    else
        echo "  nothing listening on ${PORT}"
        status_code=1
    fi
else
    echo "  (ss not available)"
fi

# --- Probes -----------------------------------------------------------------
echo
echo "== liveness (/healthz) =="
if health="$(curl -fsS --max-time 5 "${BASE}/healthz" 2>/dev/null)"; then
    echo "  200 ${health}"
else
    echo "  unreachable"
    status_code=1
fi

echo
echo "== readiness (/readyz) =="
# /readyz legitimately returns 503 before the model is warm, so a 503 is
# reported as "not ready" rather than as an error -- but it still means the
# service cannot serve traffic yet.
# curl writes the body then the status code on its own line. When the service is
# unreachable curl emits nothing at all and exits non-zero, so the fallback has
# to supply both the empty body and the "000" status.
if ready_body="$(curl -sS --max-time 5 -w '\n%{http_code}' "${BASE}/readyz" 2>/dev/null)"; then
    ready_code="$(tail -n1 <<<"${ready_body}")"
    ready_json="$(sed '$d' <<<"${ready_body}")"
else
    ready_code="000"
    ready_json=""
fi

if [[ "${ready_code}" == "200" ]]; then
    echo "  200 ${ready_json}"
elif [[ "${ready_code}" == "000" ]]; then
    echo "  unreachable"
else
    echo "  ${ready_code} ${ready_json}"
    echo "  (the model is not loaded yet; it loads on first request, or set PRELOAD_MODEL=true)"
fi

exit "${status_code}"
