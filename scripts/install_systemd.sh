#!/usr/bin/env bash
#
# Install the Laya service as a user-level systemd unit.
#
# Reads the template at deploy/systemd/laya-service.service.in, substitutes the
# paths for THIS machine, and writes the finished unit to
# ~/.config/systemd/user/laya-service.service.
#
# The substitution exists because systemd's escaping rules differ per directive
# (quoting works in ExecStart, not in WorkingDirectory, and ReadWritePaths wants
# C-style \x20). Doing it by hand is error-prone; doing it here means the
# template can ship unchanged in the repository.
#
# Usage:
#   ./scripts/install_systemd.sh            # install and reload
#   ./scripts/install_systemd.sh --enable   # install, reload, enable, and start
#   ./scripts/install_systemd.sh --dry-run  # print the unit, change nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TEMPLATE="${PROJECT_ROOT}/deploy/systemd/laya-service.service.in"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_FILE="${UNIT_DIR}/laya-service.service"

DRY_RUN=0
DO_ENABLE=0
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=1 ;;
        --enable)  DO_ENABLE=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "error: unknown argument '${arg}'" >&2
            echo "usage: $0 [--dry-run] [--enable]" >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "error: template not found at ${TEMPLATE}" >&2
    exit 1
fi

HF_CACHE="${HF_HOME:-${HOME}/.cache/huggingface}"

# Hardening toggles, read from .env. systemd cannot do this itself: it has no
# support for shell default-value syntax, so `${VAR:-true}` is rejected with
# "Failed to parse boolean value" and the directive silently falls back to its
# default. The value must be a literal by the time systemd sees it.
#
# Precedence: real environment > .env > "true" (the safe default).
read_toggle() {
    local key="$1"
    local from_env
    from_env="$(printenv "${key}" 2>/dev/null || true)"
    if [[ -n "${from_env}" ]]; then
        printf '%s' "${from_env}"
        return
    fi
    local from_file
    from_file="$(grep -E "^${key}=" "${PROJECT_ROOT}/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
    if [[ -n "${from_file}" ]]; then
        printf '%s' "${from_file}"
        return
    fi
    printf 'true'
}

NO_NEW_PRIVILEGES="$(read_toggle SYSTEMD_NO_NEW_PRIVILEGES)"
PRIVATE_DEVICES="$(read_toggle SYSTEMD_PRIVATE_DEVICES)"
PROTECT_KERNEL_MODULES="$(read_toggle SYSTEMD_PROTECT_KERNEL_MODULES)"

# Substitution uses bash parameter expansion rather than `sed`. sed interprets
# backslash escapes in the replacement text, which would mangle any path
# containing a backslash. Parameter expansion performs no escape processing, so
# the substituted text survives verbatim.
#
# No escaping of spaces is performed here: the template already wraps the
# affected values in double quotes, which is the form systemd honours for
# ReadWritePaths. (A bare space splits into two invalid paths, and \x20 is not
# an escape systemd understands -- it keeps the literal characters.)
rendered="$(cat "${TEMPLATE}")"
rendered="${rendered//@PROJECT_DIR@/${PROJECT_ROOT}}"
rendered="${rendered//@HF_CACHE@/${HF_CACHE}}"
rendered="${rendered//@NO_NEW_PRIVILEGES@/${NO_NEW_PRIVILEGES}}"
rendered="${rendered//@PRIVATE_DEVICES@/${PRIVATE_DEVICES}}"
rendered="${rendered//@PROTECT_KERNEL_MODULES@/${PROTECT_KERNEL_MODULES}}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "# Rendered from ${TEMPLATE}"
    echo "# PROJECT_DIR          = ${PROJECT_ROOT}"
    echo "# HF_CACHE             = ${HF_CACHE}"
    echo "# NoNewPrivileges      = ${NO_NEW_PRIVILEGES}"
    echo "# PrivateDevices       = ${PRIVATE_DEVICES}"
    echo "# ProtectKernelModules = ${PROTECT_KERNEL_MODULES}"
    echo "#"
    printf '%s\n' "${rendered}"
    exit 0
fi

mkdir -p "${UNIT_DIR}"
printf '%s\n' "${rendered}" > "${UNIT_FILE}"

echo "installed: ${UNIT_FILE}"
echo "  WorkingDirectory = ${PROJECT_ROOT}"
echo "  HF cache         = ${HF_CACHE}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "note: systemctl not found; the unit was written but not reloaded" >&2
    exit 0
fi

systemctl --user daemon-reload
echo "reloaded systemd --user"

# Surface any parse problems immediately rather than at first start. systemd
# logs warnings about ignored directives; show them now.
if warnings="$(systemctl --user show laya-service 2>&1 >/dev/null | grep -v '^$')"; then
    if [[ -n "${warnings}" ]]; then
        echo "systemd reported the following while loading the unit:" >&2
        printf '  %s\n' "${warnings}" >&2
    fi
fi

echo
echo "next steps:"
echo "  systemctl --user enable --now laya-service    # start now + on login"
echo "  systemctl --user status laya-service"
echo "  journalctl --user -u laya-service -f"

if [[ "${DO_ENABLE}" -eq 1 ]]; then
    echo
    echo "enabling and starting ..."
    systemctl --user enable --now laya-service
    sleep 2
    systemctl --user status laya-service --no-pager || true
fi
