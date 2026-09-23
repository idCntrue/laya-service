#!/usr/bin/env bash
#
# Post-start smoke test.
#
# Exercises the three paths an operator cares about -- liveness, readiness, and
# an authenticated inference call -- against a *running* service. It does not
# start or stop anything.
#
# Exit codes: 0 all checks passed, 1 a check failed, 2 configuration problem.
#
# Note on readiness: /readyz returns 503 until the model is warm. On a cold
# cache that first request downloads weights and can take minutes. This script
# therefore treats a 503 as a WARNING and continues to the inference check,
# which is the thing that actually proves the service works.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PORT="${PORT:-9800}"
BASE="http://127.0.0.1:${PORT}"

# --- Resolve the API key ----------------------------------------------------
if [[ -z "${LAYA_API_KEY:-}" ]] && [[ -f .env ]]; then
    # shellcheck disable=SC1091
    LAYA_API_KEY="$(grep -E '^LAYA_API_KEY=' .env | head -1 | cut -d= -f2-)"
fi

failures=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
info() { printf '  \033[36mINFO\033[0m %s\n' "$1"; }

echo "smoke test against ${BASE}"
echo

# --- 1. Liveness ------------------------------------------------------------
echo "[1/4] liveness"
code="$(curl -sS -o /tmp/laya_smoke_health.json -w '%{http_code}' --max-time 10 \
    "${BASE}/healthz" 2>/dev/null || echo 000)"
if [[ "${code}" == "200" ]]; then
    pass "/healthz -> 200 $(cat /tmp/laya_smoke_health.json)"
else
    fail "/healthz -> ${code} (is the service running? try 'make start')"
    echo
    echo "aborting: liveness failed, the service is not reachable"
    exit 1
fi

# --- 2. Readiness -----------------------------------------------------------
echo
echo "[2/4] readiness"
code="$(curl -sS -o /tmp/laya_smoke_ready.json -w '%{http_code}' --max-time 10 \
    "${BASE}/readyz" 2>/dev/null || echo 000)"
if [[ "${code}" == "200" ]]; then
    pass "/readyz -> 200 $(cat /tmp/laya_smoke_ready.json)"
elif [[ "${code}" == "503" ]]; then
    warn "/readyz -> 503 (model not loaded yet; the next check will trigger the load)"
else
    fail "/readyz -> ${code}"
fi

# --- 3. Authentication is actually enforced ---------------------------------
echo
echo "[3/4] authentication"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "${BASE}/v1/predict" \
    -H 'Content-Type: application/json' \
    -d '{"state":{},"questions":{"q":{"type":"noul","instructions":"x"}}}' 2>/dev/null || echo 000)"
if [[ "${code}" == "401" ]]; then
    pass "unauthenticated /v1/predict -> 401"
elif [[ "${code}" == "200" ]]; then
    warn "unauthenticated /v1/predict -> 200 (no API key configured; loopback-only mode)"
else
    fail "unauthenticated /v1/predict -> ${code} (expected 401)"
fi

if [[ -z "${LAYA_API_KEY:-}" ]]; then
    info "LAYA_API_KEY is empty; skipping authenticated inference checks"
    info "set it in .env, or bind HOST=127.0.0.1 for keyless local use"
    echo
    if [[ "${failures}" -eq 0 ]]; then
        echo -e "\033[32mall smoke checks passed\033[0m (inference skipped)"
        exit 0
    fi
    echo -e "\033[31m${failures} smoke check(s) failed\033[0m"
    exit 1
fi

AUTH=(-H "Authorization: Bearer ${LAYA_API_KEY}")

# --- 4. Inference -----------------------------------------------------------
echo
echo "[4/4] inference (the first call may download model weights; be patient)"
info "this can take several minutes on a cold cache -- curl timeout is 600s"

predict_body='{
  "state": {
    "observation": "Over the past 3 seconds, the x coordinate jumped discontinuously by about 2.3 meters, y was stable, heading reversed three times, and positioning confidence dropped from 0.9 to 0.4. The robot is indoors with weak GPS."
  },
  "questions": {
    "location_reliable": {
      "type": "noul",
      "instructions": "Is the current localization reliable?"
    }
  }
}'

code="$(curl -sS -o /tmp/laya_smoke_predict.json -w '%{http_code}' --max-time 600 \
    -X POST "${BASE}/v1/predict" \
    -H "Authorization: Bearer ${LAYA_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d "${predict_body}" 2>/dev/null || echo 000)"

if [[ "${code}" == "200" ]]; then
    pass "/v1/predict -> 200"
    info "response: $(head -c 400 /tmp/laya_smoke_predict.json)"
elif [[ "${code}" == "503" ]]; then
    fail "/v1/predict -> 503 (model unavailable; check the log for the load error)"
    info "response: $(head -c 400 /tmp/laya_smoke_predict.json)"
else
    fail "/v1/predict -> ${code}"
    info "response: $(head -c 400 /tmp/laya_smoke_predict.json)"
fi

# Robot-dog endpoint
code="$(curl -sS -o /tmp/laya_smoke_dog.json -w '%{http_code}' --max-time 600 \
    -X POST "${BASE}/v1/robot-dog/localization-reliability" \
    -H "Authorization: Bearer ${LAYA_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d '{"x_jump_m":2.3,"y_stable":true,"heading_reversals":3,"confidence_start":0.9,"confidence_end":0.4,"environment":"indoor_weak_gps"}' \
    2>/dev/null || echo 000)"

if [[ "${code}" == "200" ]]; then
    pass "/v1/robot-dog/localization-reliability -> 200"
    info "response: $(head -c 400 /tmp/laya_smoke_dog.json)"
else
    fail "/v1/robot-dog/localization-reliability -> ${code}"
    info "response: $(head -c 400 /tmp/laya_smoke_dog.json)"
fi

# --- Summary ----------------------------------------------------------------
echo
if [[ "${failures}" -eq 0 ]]; then
    printf '\033[32mall smoke checks passed\033[0m\n'
    exit 0
fi
printf '\033[31m%d smoke check(s) failed\033[0m\n' "${failures}"
exit 1
