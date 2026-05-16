#!/usr/bin/env bash
# PhoneForge web installer — run on the VPS (ssh ytm-vps).
#
# Idempotent: safe to re-run. Pulls latest, refreshes the venv, ensures
# the systemd unit + Caddy block are in place, and verifies the public
# endpoint responds 200 over HTTPS.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/phoneforge}"
SERVICE_NAME="phoneforge-web"
SERVICE_FILE="${REPO_DIR}/deploy/${SERVICE_NAME}.service"
CADDY_SNIPPET="${REPO_DIR}/deploy/Caddyfile.snippet"
CADDY_FILE="${CADDY_FILE:-/etc/caddy/Caddyfile}"
PUBLIC_URL="${PUBLIC_URL:-https://pf.alexera.pro}"

log() { printf '\033[36m[install]\033[0m %s\n' "$*"; }

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

# 1. Pull
log "git pull in ${REPO_DIR}"
cd "${REPO_DIR}"
git pull --ff-only

# 2. Ensure venv exists
if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
    log "creating venv"
    python3.12 -m venv "${REPO_DIR}/.venv"
fi
log "installing deps"
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip wheel >/dev/null
"${REPO_DIR}/.venv/bin/pip" install -e "${REPO_DIR}[dev]"

# 3. Ensure SESSION_SECRET in .env
if ! grep -q '^SESSION_SECRET=' "${REPO_DIR}/.env" 2>/dev/null; then
    log "generating SESSION_SECRET (writing to .env)"
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    printf '\nSESSION_SECRET=%s\n' "${SECRET}" >> "${REPO_DIR}/.env"
    chmod 600 "${REPO_DIR}/.env"
fi
if ! grep -q '^PHONEFORGE_PIN=' "${REPO_DIR}/.env" 2>/dev/null; then
    log "setting PHONEFORGE_PIN=1991 (override in .env if you want a different one)"
    printf 'PHONEFORGE_PIN=1991\n' >> "${REPO_DIR}/.env"
fi

# 4. systemd unit
log "installing systemd unit"
cp "${SERVICE_FILE}" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

# 5. Caddy snippet — idempotent merge
log "ensuring Caddy block for pf.alexera.pro"
if ! grep -q 'pf\.alexera\.pro' "${CADDY_FILE}" 2>/dev/null; then
    printf '\n' >> "${CADDY_FILE}"
    cat "${CADDY_SNIPPET}" >> "${CADDY_FILE}"
    log "appended snippet"
else
    log "snippet already present (skipping)"
fi

# Validate before reload — Caddy reload-fails-but-still-running is a silent footgun.
log "validating Caddyfile"
caddy validate --config "${CADDY_FILE}"

log "reloading Caddy"
systemctl reload caddy

# 6. Smoke test
log "waiting for service…"
sleep 2
if curl -fsS "${PUBLIC_URL}/health" >/dev/null; then
    log "OK — ${PUBLIC_URL}/health responded"
else
    echo "ERROR — ${PUBLIC_URL}/health did not respond" >&2
    journalctl -u "${SERVICE_NAME}" --since='1 min ago' --no-pager | tail -30 || true
    exit 1
fi

log "done. systemctl status ${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager || true
