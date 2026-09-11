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
# Timers are enabled on every deploy, not just installed -- enabling is
# idempotent, so one that was never switched on gets switched on rather than
# waiting to be noticed.
#
# Two, and the second one is a correction.
#
# cfdinvest.service is a resident daemon that owns every job there is:
# quotes, the advisor, dealing, ingestion, the daily run, both snapshots and
# pruning. The PIPELINE timer is genuinely superseded by it -- adding it
# would run ingestion twice on two schedules against one lake -- and its
# unit file survives only as history.
#
# **The MONITOR timer is not superseded, and treating it as though it were
# cost two silent stalls.** It runs `monitor --alert` in a SEPARATE process,
# which is the entire point: the daemon's own monitoring snapshot runs
# inside the loop that can hang, so when the loop hangs the snapshot hangs
# with it. On 2026-09-09 that was a 45-minute stall nobody was told about;
# on 2026-09-11 it was nineteen hours, through most of a trading session,
# with `check_dealing_liveness` sitting in the tree unable to fire.
#
# A monitor inside the thing it monitors is not a monitor. This one runs
# every 30 minutes, out of process, and reads without ingesting.
#
# **The DEALER timer is the third, and it is the same lesson again.** Dealing
# was a job in the daemon's sequential loop, so the lightest and most
# time-critical work in the system queued behind the heaviest. When the
# pipeline wedged on 2026-09-11 the account could not be closed for an entire
# session. It now runs every five minutes in its own process, and
# `trade-capital` has been removed from the daemon's job list so the two
# cannot both deal.
TIMERS=(cfdinvest.timer cfdinvest-monitor.timer cfdinvest-dealer.timer)
LOG_TAG="cfdinvest-deploy"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | $*" | systemd-cat -t "${LOG_TAG}" -p info; echo "$*"; }
fail() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | FAILED: $*" | systemd-cat -t "${LOG_TAG}" -p err; echo "FAILED: $*" >&2; }

deploy_steps() {
  cd "${REPO_DIR}" || { fail "no ${REPO_DIR}"; return 1; }

  # The tree is owned by the service user and this runs as root, so git
  # refuses it as "dubious ownership" unless told the directory is expected.
  # Declared here rather than in a one-off command, so a rebuilt server does
  # not need somebody to remember it.
  git config --global --add safe.directory "${REPO_DIR}" 2>/dev/null || true

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

  # The broker rules table. One file in the repository is the source; the
  # live copy is installed from it, so the two cannot drift. It used to be
  # gitignored and copied by hand, which is the same class of mistake as
  # copying code and not restarting.
  local rules_src="${REPO_DIR}/config/t212_rules.toml"
  local rules_dst="/var/lib/cfdinvest/.config/cfdinvest/t212_rules.toml"
  if [[ -f "${rules_src}" ]]; then
    if ! cmp -s "${rules_src}" "${rules_dst}" 2>/dev/null; then
      log "Installing the Trading 212 rules table"
      install -o cfdinvest -g cfdinvest -m 640 -D "${rules_src}" "${rules_dst}"         || fail "install rules table"
    fi
  fi

  # The systemd units. Installed from the repository for the same reason the
  # rules table is: a unit edited by hand on the box is a change nobody can
  # review and nobody can reproduce. Timers are enabled here rather than
  # documented, because a unit that exists but was never enabled does
  # nothing at all -- which is precisely how the artefact store grew until
  # the box reached 17% free with a working prune command sitting unused.
  local reload=0
  for unit_src in "${REPO_DIR}"/deployment/systemd/*.{service,timer,path}; do
    [[ -f "${unit_src}" ]] || continue
    local unit_name unit_dst
    unit_name="$(basename "${unit_src}")"
    unit_dst="/etc/systemd/system/${unit_name}"
    if ! cmp -s "${unit_src}" "${unit_dst}" 2>/dev/null; then
      log "Installing ${unit_name}"
      install -m 644 -D "${unit_src}" "${unit_dst}" || fail "install ${unit_name}"
      reload=1
    fi
  done
  if [[ "${reload}" == "1" ]]; then
    systemctl daemon-reload || fail "daemon-reload"
  fi

  # Enabling is idempotent, so this runs every time and a timer that was
  # never switched on gets switched on rather than waiting to be noticed.
  for timer in "${TIMERS[@]}"; do
    if ! systemctl is-enabled --quiet "${timer}" 2>/dev/null; then
      log "Enabling ${timer}"
      systemctl enable --now "${timer}" || fail "enable ${timer}"
    fi
  done

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

  for timer in "${TIMERS[@]}"; do
    if systemctl is-active --quiet "${timer}"; then
      log "  ${timer}: active"
    else
      fail "  ${timer}: NOT active"; ok=1
    fi
  done

  # Disk. The daemon prunes after each daily run, which keeps the artefact
  # store bounded at a few hundred megabytes, so this figure is the evidence
  # that it is still doing so -- and it also catches the case that actually
  # bit, which was another tenant on the box rather than this one. Reported
  # every deploy rather than waited for: the dashboard showed 17% free
  # before anyone looked.
  local free_pct
  free_pct="$(df --output=pcent /var/lib/cfdinvest | tail -1 | tr -dc '0-9')"
  if [[ -n "${free_pct}" ]]; then
    log "  disk: ${free_pct}% used on /var/lib/cfdinvest"
    if (( free_pct >= 85 )); then
      fail "  disk above 85% used -- pruning is not keeping up"
      ok=1
    fi
  fi

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
