# Deployment

The dashboard at `https://cfd.example.com`, and keeping the demo account's
numbers away from the real one's.

```
deployment/
    caddy/Caddyfile                     HTTPS, basic auth, reverse proxy
    systemd/cfdinvest-dashboard.service Streamlit, bound to localhost
    systemd/cfdinvest-pipeline.service  one run, driven by its timer
    systemd/cfdinvest-monitor.service   the health check, on its own timer
    scripts/make-basicauth-hash.sh      the only way a password is handled
    scripts/cfdinvest.env.example       every variable, no values
    scripts/logrotate-cfdinvest
```

---

## No password is in this repository, and none can be

The Caddyfile reads `{$CFD_BASIC_AUTH_USER}` and `{$CFD_BASIC_AUTH_HASH}` from
the environment. The hash goes in `/etc/default/caddy`; the password goes
nowhere at all.

```bash
./deployment/scripts/make-basicauth-hash.sh
```

It reads the password with `read -rs`, so it never reaches the terminal, the
scrollback or the shell history — which is also why it is not taken as a
command-line argument, where it would sit in `ps` for anyone on the box.

Application secrets — the SMTP password, the FRED key — live in
`/etc/cfdinvest/env`, mode `0640`, `root:cfdinvest`. `cfdinvest/env.py` reads
that file and **never logs a value**: `Environment` redacts on `repr`, and the
Configuration page shows `set` or `not set` rather than a prefix. A redaction
that leaks four characters and a length makes a short password guessable.

The env parser is not a shell. A value containing `$(...)` is that literal
string — the file holds passwords, and a parser that could execute them would
be a way to execute them.

---

## The load-bearing argument

```
--server.address 127.0.0.1
```

Without it, Streamlit binds every interface and the dashboard answers directly
on port 8501 — going round Caddy, and therefore round the basic auth. The
proxy is only a boundary because the app is unreachable any other way.

---

## Setting it up

```bash
# 1. Code and virtualenv
sudo mkdir -p /opt/cfdinvest /var/lib/cfdinvest /etc/cfdinvest
sudo useradd --system --home /opt/cfdinvest --shell /usr/sbin/nologin cfdinvest
sudo chown -R cfdinvest:cfdinvest /opt/cfdinvest /var/lib/cfdinvest
sudo -u cfdinvest git clone https://github.com/brent-jenkins/cfdinvest /opt/cfdinvest
sudo -u cfdinvest python3 -m venv /opt/cfdinvest/.venv
sudo -u cfdinvest /opt/cfdinvest/.venv/bin/pip install -e '/opt/cfdinvest[ui]'

# 2. Secrets
sudo cp deployment/scripts/cfdinvest.env.example /etc/cfdinvest/env
sudo $EDITOR /etc/cfdinvest/env
sudo chown root:cfdinvest /etc/cfdinvest/env && sudo chmod 0640 /etc/cfdinvest/env

# 3. Basic auth
./deployment/scripts/make-basicauth-hash.sh        # paste output into /etc/default/caddy
sudo cp deployment/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

# 4. Services
sudo cp deployment/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cfdinvest-dashboard
sudo systemctl enable --now cfdinvest-pipeline.timer
sudo systemctl enable --now cfdinvest-monitor.timer
```

Point the DNS `A` record for `cfd.example.com` at the box first. Caddy obtains
the certificate on first request, and cannot if the name does not resolve.

---

## Two details worth knowing

**`SuccessExitStatus=0 1` on the monitor unit.** A red status exits `1`. That
is the check working, not the unit failing — without this line systemd marks
the service failed every time it does its job, and a permanently-failed unit
is one nobody looks at.

**The pipeline timer is in UTC.** `Mon..Fri 21:30 UTC` is after the US cash
close on both sides of the daylight-saving change. A wall-clock schedule drifts
by an hour twice a year, and the drift is invisible until a run lands
mid-session. For session-derived times, `cfdinvest-schedule crontab` prints
them from the market calendar.

---

## Virtual and live

```bash
CFD_ACCOUNT_MODE=virtual   # a Trading 212 demo account
CFD_ACCOUNT_MODE=live      # real money
```

Everything downstream is kept in separate trees:

```
/var/lib/cfdinvest/
    raw/  metadata/  processed/      <- the lake, shared
    virtual/
        runs/  confirmations/  monitoring/  risk/  assessments/
    live/
        runs/  confirmations/  monitoring/  risk/  assessments/
```

**The lake is shared, and this is a deliberate departure from the spec.** Raw
prices are the same prices whichever account you trade them in. Keeping two
identical copies would double the disk, double the ingestion, and burn the
FRED rate limit to produce two files that must agree and could drift. What
separates is everything that carries a *result*: runs, confirmations,
analytics, risk, monitoring.

Emails carry the mode in the subject:

```
[cfdinvest] open_long SPX500 (virtual)
[cfdinvest] RED — 0.4 GB free (live)
```

Both deployments send to the same inbox. An alert about a demo account that
reads identically to one about real money is an alert that will eventually be
acted on as the wrong one.

The dashboard sidebar shows which account it is reading, and says **LIVE — real
money** in red when it is. Every number on every page changes meaning between
the two.

---

## Separation is opt-in, and here is why

With `CFD_ACCOUNT_MODE` unset, everything reads and writes the shared root,
exactly as before this existed.

Turning it on by default would strand every artefact written so far one
directory above where the readers now look. The dashboard would come back
empty, the analytics would report no runs, and a lake that appears to have lost
its history is a bad way to find out about a new feature.

To migrate an existing install:

```bash
sudo systemctl stop cfdinvest-dashboard cfdinvest-pipeline.timer
cd /var/lib/cfdinvest
sudo -u cfdinvest mkdir -p virtual
sudo -u cfdinvest mv runs confirmations monitoring risk assessments virtual/ 2>/dev/null
# leave raw/ metadata/ processed/ where they are -- the lake is shared
echo 'CFD_ACCOUNT_MODE=virtual' | sudo tee -a /etc/cfdinvest/env
sudo systemctl start cfdinvest-dashboard cfdinvest-pipeline.timer
```

Check it before trusting it:

```bash
sudo -u cfdinvest /opt/cfdinvest/.venv/bin/cfdinvest-schedule monitor
```

A run that has gone missing shows up as `No pipeline run has ever been
recorded` — which is the monitoring layer earning its place.

---

## Running the live account

A second deployment, or the same one with the variable changed. If both run on
one box, give them separate units with separate `EnvironmentFile`s rather than
switching a shared one: a variable that means "which account" and gets edited
in place is a variable that will one day be wrong for a run already in flight.

Nothing in this system places an order in either mode. `live` means the
*results* describe an account with real money in it, and that you are typing
the trades into Trading 212 yourself.

---

## Tests

```bash
python -m pytest tests/test_deployment.py -q
```

79 tests. These are configuration files nobody executes in CI, so a typo in
one is otherwise found by a stranger:

- The Caddyfile has a `basic_auth` block and proxies to `127.0.0.1:8501`.
- Every unit binds localhost, runs as the service user, and reads its secrets
  from `EnvironmentFile` rather than from a command line.
- **No file under `deployment/` sets anything that looks like a password** —
  every secret-shaped assignment must be empty or a reference.
- The hash script never takes a password as an argument.
- Confirmations, scheduler state, monitoring and risk snapshots do not cross
  between accounts, and the run readers follow the account they are given.
- The account is part of the dashboard's cache key, so switching re-reads
  rather than relabelling.

Mutation-checked: putting a real password in the env example, inlining the
bcrypt hash in the Caddyfile, binding `0.0.0.0`, ignoring the resolved account
in `account_root`, dropping the account from the run readers or from the cache
key, and dropping the account from email subjects each make the suite fail.

---

## Deploying

A push to `main` deploys. GitHub posts to `https://cfd.example.com/webhook/`,
Caddy proxies it to a loopback listener, the listener checks the HMAC and
writes a request file, and a separate root unit does the work.

This exists because of a specific failure. On 2026-08-25 the code was copied
to the server and the services were not restarted. A resident daemon went on
executing the modules it had imported at start-up, so a change made at 21:47
was still not running at 08:00 the next morning — and the run recorded
`fx_enabled=false` in its own metadata, where nobody looked. **Copying files
is not deploying.** The deploy always restarts, and then checks what it
produced.

### The pieces

| | |
| --- | --- |
| `cfdinvest-webhook.service` | Unprivileged, confined, loopback 8602. Verifies the signature, writes `/run/cfdinvest/deploy.request`, and nothing else. |
| `cfdinvest-deploy.path` | Watches that file. |
| `cfdinvest-deploy.service` | Root, oneshot. Runs `deploy.sh`. Started by the watcher and by nothing else. |

The split is the point. The other two apps on this machine run their listeners
as root because each spawns its own deploy script; a process reachable from
the internet that runs a shell as root is one signature-check bug away from
being a shell on the box. Here the reachable half holds no privilege, and the
privileged half is unreachable — it takes no input and reads nothing from the
request but the fact that it changed.

It also means `deploy.sh` can restart the listener as its final step without
killing itself, so `KillMode=process` is not needed. The other two need it,
and that bug was found the hard way.

### What deploy.sh does

Fetches and hard-resets to `origin/main`; reinstalls only if `pyproject.toml`
moved; installs `config/t212_rules.toml` to `~/.config/cfdinvest/` if it
differs; restarts `cfdinvest` and `cfdinvest-dashboard`; then verifies both
units are active, the dashboard answers 200, and — the check that would have
caught the incident above — reads the strategy settings back **out of the
running process** rather than off the disk.

### One-time setup

1. **Deploy key.** `/root/.ssh/id_ed25519_cfdinvest`, with a `github-cfdinvest`
   alias in `/root/.ssh/config` so the remote URL names that key and no other.
   The public half goes in the repository's *Deploy keys*, read-only. Per
   repository, never shared with the other apps.
2. **Webhook secret.** `CFD_GITHUB_WEBHOOK_SECRET` in `/etc/cfdinvest/env`.
   This repository's own — deliberately not the `GITHUB_WEBHOOK_SECRET` the
   other two share, because one compromise reaching all three services is a
   bad trade for saving a line.
3. **The webhook.** Repository settings → Webhooks → payload URL
   `https://cfd.example.com/webhook/`, content type `application/json`, secret
   as above, "Just the push event".

### Checking it

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://cfd.example.com/webhook/    # 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' \
  https://cfd.example.com/webhook/                                           # 401
journalctl -u cfdinvest-webhook -n 50
journalctl -t cfdinvest-deploy -n 100
```

An unsigned POST must return 401. If it returns anything else, stop and fix
that before pushing anything.
