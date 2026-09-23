#!/usr/bin/env bash
#
# Tail the service log.
#
# Logs are JSON, one object per line. When `jq` is available the lines are
# pretty-printed and the noisy startup banner is filtered out; otherwise the raw
# JSON is streamed, which is still greppable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

LOG_FILE="${LOG_FILE:-logs/laya.log}"
LINES="${LINES:-50}"

if [[ ! -f "${LOG_FILE}" ]]; then
    echo "no log file at ${LOG_FILE}" >&2
    echo "has the service been started with 'make start'?" >&2
    exit 1
fi

echo "tailing ${LOG_FILE} (ctrl-c to stop)" >&2

if command -v jq >/dev/null 2>&1; then
    tail -n "${LINES}" -f "${LOG_FILE}" \
        | jq -Rr 'fromjson? // .' 2>/dev/null
else
    echo "(install jq for pretty output)" >&2
    tail -n "${LINES}" -f "${LOG_FILE}"
fi
