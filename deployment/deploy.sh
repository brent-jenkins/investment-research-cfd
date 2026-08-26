#!/bin/bash
# Pull, install, restart, and prove it took.
#
# The restart is the point. On 2026-08-25 the code was copied to this server
# and the services were left running; a resident daemon went on executing the
# modules it had imported at start-up, so a change that had been made forty
# minutes earlier was still not running eleven hours later. It reported
# fx_enabled=false into its own metadata and nobody looked. Copying files is
# not deploying.
#
# The verification at the end is the second half of that lesson. A deploy that
# does not check what it produced is a deploy that can fail quietly, which is
# the only kind that gets missed.

set -uo pipefail

REPO_DIR="/opt/cfdinvest"
VENV="${REPO_DIR}/.venv"
SERVICES=(cfdinvest cfdinvest-dashboard)
LOG_TAG="cfdinvest-deploy"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | $*" | systemd-cat -t "${LOG_TAG}" -p info; echo "$*"; }
fail() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | FAILED: $*" | systemd-cat -t "${LOG_TAG}" -p err; echo "FAILED: $*" >&2; }

deploy_steps() {
  cd "${REPO_DIR}" || { fail "no ${REPO_DIR}"; return 1; }

  log "Fetching origin/main"
  local before
  before="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git fetch --quiet origin main || { fail "git fetch"; return 1; }
  git reset --hard --quiet origin/main || { fail "git reset"; return 1; }
  local after
  after="$(git rev-parse HEAD)"
  log "Now at ${after} (was ${before})"

  # Only reinstall when packaging metadata moved. An editable install picks
  # up source changes on its own; entry points and dependencies do not.
  if ! git diff --quiet "${before}" "${after}" -- pyproject.toml 2>/dev/null; then
    log "pyproject.toml changed -- reinstalling"
    "${VENV}/bin/pip" install --quiet -e "${REPO_DIR}[ui]" || { fail "pip install"; return 1; }
  fi

  # Restart every resident process. Both hold imported modules in memory and
  # neither notices a file changing underneath it.
  for service in "${SERVICES[@]}"; do
    log "Restarting ${service}"
    systemctl restart "${service}" || { fail "restart ${service}"; return 1; }
  done

  sleep 4

  # -- prove it -------------------------------------------------------------
  local ok=0
  for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${service}"; then
      log "  ${service}: active"
    else
      fail "  ${service}: NOT active"; ok=1
    fi
  done

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8502/ || echo 000)"
  if [[ "${code}" == "200" ]]; then
    log "  dashboard: HTTP ${code}"
  else
    fail "  dashboard: HTTP ${code}"; ok=1
  fi

  # The check that would have caught the incident this script exists to
  # prevent: ask the running code what it thinks its own settings are,
  # rather than trusting that the file on disk is what is executing.
  local settings
  settings="$(sudo -u cfdinvest "${VENV}/bin/python" -c "
from cfdinvest.config import load_config
s = load_config().strategy
print(f'fx={s.fx_enabled} regime={s.regime_mode} time_stop={s.time_stop_days}')
" 2>/dev/null)"
  if [[ -n "${settings}" ]]; then
    log "  running config: ${settings}"
  else
    fail "  could not read the running config"; ok=1
  fi

  return "${ok}"
}

log "=== deploy starting ==="
if deploy_steps; then
  log "=== deploy finished ==="
else
  fail "=== deploy finished with errors ==="
fi

# The listener is restarted last, so a change to webhook_listener.py takes
# effect too. This is safe here only because of the split: the deploy runs
# in cfdinvest-deploy.service, not as a child of the listener, so stopping
# the listener cannot truncate the deploy. The other two listeners on this
# machine do spawn their own deploy scripts and need KillMode=process to
# avoid exactly that; this one does not, which is a second reason to prefer
# the arrangement.
log "Restarting the webhook listener"
systemctl restart cfdinvest-webhook || fail "restart cfdinvest-webhook"
