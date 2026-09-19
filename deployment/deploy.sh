#!/bin/bash
# Pull, install, restart, and prove it took.
#
# THE SCRIPT RUNS FROM A PINNED COPY, AND THEN RE-EXECS THE PULLED ONE.
#
# Two problems, one cause: this file deploys itself.
#
# Bash reads a script INCREMENTALLY, so `git reset --hard` rewriting
# deploy.sh underneath a running deploy.sh can make it resume at a byte
# offset in different text. That has not bitten yet and is only a matter of
# time, so the first thing this does is copy itself to /tmp and exec that.
#
# The visible half did bite, twice on 2026-09-12: the deploy runs the
# version already on disk and pulls afterwards, so a change to deploy.sh
# only took effect on the NEXT deploy. A guard added that morning failed the
# deploy on correct configuration, was fixed within minutes, and failed
# again on the very next push because the box was still running the old
# copy. New timers had to be enabled by hand for the same reason.
#
# So after the pull, if deploy.sh itself changed, the new one is exec'd --
# once, guarded against a loop.
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

# Pin: run from a copy so a pull cannot rewrite the file being executed.
if [[ -z "${CFDINVEST_DEPLOY_PINNED:-}" ]]; then
  _pinned="$(mktemp /tmp/cfdinvest-deploy.XXXXXX.sh)"
  cp -- "$0" "${_pinned}"
  chmod +x "${_pinned}"
  export CFDINVEST_DEPLOY_PINNED="${_pinned}"
  # The pinned copy removes itself; a deploy that dies must not leave litter
  # in /tmp on a box whose disk is checked by this very script.
  exec "${_pinned}" "$@"
fi
trap 'rm -f -- "${CFDINVEST_DEPLOY_PINNED}"' EXIT

REPO_DIR="/opt/cfdinvest"
VENV="${REPO_DIR}/.venv"
# Resident processes, restarted and then asserted ACTIVE.
#
# `cfdinvest` is deliberately NOT here any more. It became Type=oneshot on
# 2026-09-11 -- one tick, then it exits -- so four seconds after a restart it
# is correctly `inactive (dead)`, and asserting is-active would fail every
# deploy on a perfectly healthy box. It is verified as a ONESHOT below
# instead, which is a stronger check: it proves a pass actually RAN and
# returned zero, where is-active only ever proved a process existed.
SERVICES=(cfdinvest-dashboard)

# Timer-driven one-shots. Started synchronously and asserted to have
# SUCCEEDED, rather than to be running.
ONESHOTS=(cfdinvest cfdinvest-quotes cfdinvest-ingest cfdinvest-daily)
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
TIMERS=(cfdinvest.timer cfdinvest-monitor.timer cfdinvest-dealer.timer cfdinvest-quotes.timer cfdinvest-ingest.timer cfdinvest-daily.timer)
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
  # Carried across a re-exec, not re-read. The re-exec happens AFTER the
  # pull, so a second run would compute before == after and every
  # "did this change?" test below would answer no.
  #
  # That is not hypothetical: on 2026-09-14 a commit changed deploy.sh and
  # pyproject.toml together, the script re-exec'd itself, the reinstall test
  # compared c9db193 against c9db193, and psutil was never installed. The
  # deploy reported success throughout. Found because the package was absent
  # afterwards, not because anything said so.
  before="${CFDINVEST_DEPLOY_BEFORE:-$(git rev-parse HEAD 2>/dev/null || echo none)}"
  git fetch --quiet origin main || { fail "git fetch"; return 1; }

  # THE SESSION GUARD, and it sits HERE deliberately: after the fetch and
  # before the reset.
  #
  # "Do not deploy during a session" has been this project's rule since
  # 2026-09-10, when nine deploys cost roughly four dealing passes. Nothing
  # enforced it, and on 2026-09-14 two deploys landed at 19:25 and 19:39 UTC
  # -- thirty-five and twenty-one minutes before the close, market open --
  # because a push to main deploys within seconds and nobody chose that.
  #
  # Refusing AFTER the fetch is what makes the refusal visible. origin/main
  # is updated on disk and HEAD is not, so the box is measurably behind the
  # repository and `check_deploy_is_current` says so on the next monitoring
  # pass. Refusing before the fetch would leave the two identical and the
  # deferred deploy would be silent -- which is the failure this guard would
  # otherwise introduce, and a worse one than the deploy it prevents.
  #
  # UNKNOWN IS NOT CLOSED. `market-open` exits 1 only when it has positively
  # established that every market is shut; anything else -- open, no
  # universe, an unreadable calendar -- refuses. A guard that waves the
  # deploy through when it cannot tell is the inert gate this file's own
  # history is full of.
  if [[ -z "${CFDINVEST_DEPLOY_INTO_OPEN_MARKET:-}" ]]; then
    local session=0
    sudo -u cfdinvest "${VENV}/bin/cfdinvest-schedule" market-open >/dev/null 2>&1       || session=$?
    if [[ "${session}" -ne 1 ]]; then
      local why="a market in the universe is open"
      [[ "${session}" -eq 3 ]] && why="the session calendar could not be read"
      fail "refusing to deploy: ${why}"
      log "  The repository is at $(git rev-parse --short origin/main) and this"
      log "  box stays at $(git rev-parse --short HEAD). Nothing was changed."
      log "  Monitoring reports the box as behind until a deploy succeeds."
      log "  It will go out on the next deploy after the close, or now with:"
      log "    CFDINVEST_DEPLOY_INTO_OPEN_MARKET=1 bash deployment/deploy.sh"
      log "  Override only for something actively placing wrong trades --"
      log "  which is the single reason the 2026-09-10 deploys were right."
      return 1
    fi
    # Say so on the way THROUGH as well, not only on the way back. A guard
    # that is silent when it permits cannot be told apart from a guard that
    # has stopped running -- which is this project's oldest failure, "a gate
    # reporting success while doing nothing", and the reason the completed-
    # bar trim shipped inert twice in one afternoon. The first deploy after
    # this guard went in left no trace that it had run at all.
    log "Session guard: every market closed; deploying"
  else
    log "SESSION GUARD OVERRIDDEN -- deploying into a live market deliberately"
  fi

  git reset --hard --quiet origin/main || { fail "git reset"; return 1; }
  local after
  after="$(git rev-parse HEAD)"
  log "Now at ${after} (was ${before})"

  # If this pull changed deploy.sh, run the NEW one rather than finishing
  # the old. Guarded so it can happen at most once: a script that re-execs
  # unconditionally is a deploy loop, and a deploy loop against a vendor is
  # worse than a stopped deploy because it looks like it is working.
  if [[ -z "${CFDINVEST_DEPLOY_REEXEC:-}" ]]      && ! cmp -s "${CFDINVEST_DEPLOY_PINNED}" "${REPO_DIR}/deployment/deploy.sh"; then
    log "deploy.sh changed in this pull -- re-running the new one"
    export CFDINVEST_DEPLOY_REEXEC=1
    # The revision we STARTED from, so the new run can still tell what this
    # pull changed. Without it the successor sees before == after and skips
    # the reinstall -- see the comment where `before` is read.
    export CFDINVEST_DEPLOY_BEFORE="${before}"
    unset CFDINVEST_DEPLOY_PINNED
    rm -f -- "${_pinned:-}" 2>/dev/null || true
    exec "${REPO_DIR}/deployment/deploy.sh" "$@"
  fi

  # Only reinstall when packaging metadata moved. An editable install picks
  # up source changes on its own; entry points and dependencies do not.
  if ! git diff --quiet "${before}" "${after}" -- pyproject.toml 2>/dev/null; then
    log "pyproject.toml changed -- reinstalling"
    # `monitoring` alongside `ui`: psutil is optional to the LIBRARY and
    # wanted on the DEPLOYMENT, which is what an extra is for. Without it the
    # monitor reports CPU and memory as "not measured" -- honest, and blind in
    # exactly the place that cost the 2026-09-09 throttle and the 2026-09-11
    # stall.
    "${VENV}/bin/pip" install --quiet -e "${REPO_DIR}[ui,monitoring]" \
      || { fail "pip install"; return 1; }
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

  # Restart every resident process. It holds imported modules in memory and
  # does not notice a file changing underneath it.
  for service in "${SERVICES[@]}"; do
    log "Restarting ${service}"
    systemctl restart "${service}" || { fail "restart ${service}"; return 1; }
  done

  # Run each one-shot once, now, against the code just installed. `start` on
  # a Type=oneshot blocks until it finishes, so this is the new code actually
  # doing a pass rather than a promise that it would.
  for service in "${ONESHOTS[@]}"; do
    log "Running ${service}"
    systemctl start "${service}" || fail "start ${service}"
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

  # **The running limits must come from the unit files, not from a console
  # command.** `systemctl set-property` writes to /etc/systemd/system.control
  # and SILENTLY OVERRIDES the unit, and it survives deploys, reboots and
  # daemon-reload. On 2026-09-12 the box was found running MemoryHigh=5G and
  # MemoryMax=6G on cfdinvest while the shipped unit set MemoryMax=2G and no
  # MemoryHigh at all -- left over from raising the limit by hand during the
  # 2026-09-11 stall.
  #
  # MemoryHigh is the directive that caused the 2026-09-09 stall: it
  # throttles rather than failing, so the process never dies, Restart never
  # fires, the unit stays active and the account silently stops dealing. A
  # test in the repository asserts the FILE does not set it, and that test
  # passed for a day while production did. Only the box can check the box.
  # Scoped to the ONE-SHOTS, which are the units that decide and deal.
  #
  # cfdinvest-dashboard is deliberately exempt and sets MemoryHigh=700M in
  # its own unit file, with a reason: throttling a read-only Streamlit UI
  # makes a page slow, while throttling the thing that decides or deals is
  # what stopped the account twice without anything failing. The first
  # version of this check covered SERVICES too and failed the deploy on that
  # legitimate setting -- a guard that cries wolf on correct configuration
  # gets switched off, which is how the monitor timer came to be disabled.
  for unit in "${ONESHOTS[@]}"; do
    high=$(systemctl show "${unit}" -p MemoryHigh --value)
    if [[ "${high}" != "infinity" && -n "${high}" ]]; then
      fail "  ${unit}: MemoryHigh=${high} is in force; it throttles instead of failing"
      fail "    run: systemctl revert ${unit}"
      ok=1
    fi
  done
  if [[ -d /etc/systemd/system.control ]]      && compgen -G "/etc/systemd/system.control/cfdinvest*" >/dev/null; then
    fail "  a set-property override is in force and outranks the unit files:"
    systemctl show cfdinvest -p DropInPaths --value | while read -r p; do
      [[ -n "${p}" ]] && fail "    ${p}"
    done
    ok=1
  fi

  # A one-shot is proved by its RESULT, not by being active -- a finished
  # pass is `inactive (dead)` and that is the healthy state.
  for service in "${ONESHOTS[@]}"; do
    result=$(systemctl show "${service}" -p Result --value)
    status=$(systemctl show "${service}" -p ExecMainStatus --value)
    if [[ "${result}" == "success" && "${status}" == "0" ]]; then
      log "  ${service}: last pass succeeded"
    else
      fail "  ${service}: last pass ${result}, exit ${status}"; ok=1
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
