# Moving the deployment onto Python 3.14

A runbook, written 2026-09-08 for execution on a **Saturday**. Every command
is here so the day is execution rather than rediscovery.

Nothing is broken. Do not treat this as urgent.

---

## Why

**Production runs a release candidate.** `/usr/bin/python3.11` on the box is
`3.11.0~rc1-1~22.04`, and `apt` cannot fix it — Ubuntu 22.04 shipped 3.11 as
a *preview* and the candidate in `jammy-updates` is still
`3.11.0~rc1-1~22.04.1`. There is no final 3.11 available to this OS.

**Development and production disagree on more than the interpreter.**
`pyproject.toml` specifies floors (`pandas>=2.2`), not pins, so every install
drifts on its own. As of 2026-09-08 production is *ahead* of the machine the
tests run on:

|            | dev (3.14.4) | prod (3.11.0rc1) |
| ---------- | ------------ | ---------------- |
| pandas     | 3.0.3        | **3.0.5**        |
| pyarrow    | 24.0.0       | **25.0.1**       |
| streamlit  | 1.58.0       | **1.62.0**       |
| yfinance   | 1.4.1        | **1.6.0**        |
| numpy      | 2.4.6        | 2.4.6            |
| requests   | 2.34.2       | 2.34.2           |

A suite that passes on one interpreter and one set of libraries, guarding an
account that runs on another, is this project's own recurring failure wearing
different clothes: the thing measured is not the thing that runs. Closing it
is the real reason for this work. The release candidate is the prompt.

## Why BEFORE the OS upgrade, not after

The intuition is to fold this into a 22.04 → 24.04 upgrade. That is backwards
for two reasons.

**The OS upgrade does not deliver 3.14.** Noble ships python3.12. Afterwards
you would still need a standalone interpreter to match dev, having already
done the larger, riskier thing.

**And the app would not survive the upgrade as it stands:**

```
/opt/cfdinvest/.venv/bin/python3.11 -> /usr/bin/python3.11
```

The venv is a symlink into the system Python, and `python3.11` is not in the
noble archive. `do-release-upgrade` removes it, the symlink dangles, and both
services fail to start — with 523 MB of venv to rebuild under time pressure
on a half-migrated box.

A standalone interpreter severs that dependency. After this runbook the venv
no longer cares what the OS ships, and the OS upgrade becomes routine. **22.04
standard support ends April 2027**, so schedule that separately and without
pressure.

## Why a Saturday and not a market holiday

The first attempt at this was going to be Labor Day, 2026-09-08, on the
argument that US markets were shut. That argument was wrong, and the
correction is worth keeping: **2026-09-09 was the first live day of the
day-trading configuration**, and the forward record on the demo account is
the only trustworthy evidence this project holds. An interpreter change the
night before gives any oddity in that first week two candidate explanations —
strategy or runtime — which is precisely the confound this project keeps
having to unpick.

Market hours were never the binding constraint. A clean first week was.

---

## Before you start

- [ ] Markets closed and no dealing pass due (`cfdinvest-schedule monitor`)
- [ ] `git -C /opt/cfdinvest status --porcelain` is clean, or you know why not
- [ ] You have the ssh key: `/c/GitHub/ssh-keys/ssh-key-2026-06-14.key`
- [ ] Note the current balance and open positions, so a surprise afterwards
      is attributable

```bash
KEY=/c/GitHub/ssh-keys/ssh-key-2026-06-14.key
BOX=root@your-server
ssh -i "$KEY" $BOX "systemctl is-active cfdinvest cfdinvest-dashboard; \
  git -C /opt/cfdinvest log --oneline -1; df -h / | tail -1"
```

Disk was 21% used with 29 GB free on 2026-09-08. A second venv needs about
another 500 MB; if free space is under 3 GB, stop and clear room first.

---

## Step 1 — pin the library versions first, in a separate commit

Do this **locally, before touching the box**, so the interpreter change and
the dependency change are separable if something goes wrong. Pin to what
production already runs, not to what dev runs: production's versions are
newer and have been exercised against the real lake.

Edit `pyproject.toml` `dependencies` from floors to pins, then locally:

```bash
python -m pip install -e ".[ui]" --upgrade
pytest -q                       # must be green on 3.14 with prod's versions
```

Commit. **Do not push yet** — pushing triggers the deploy webhook, which
would reinstall into the *old* venv. Push at Step 5.

> If the suite fails here, stop. That is a genuine dev/prod divergence and it
> is better found on a Saturday than inferred from a strange Tuesday.

## Step 2 — install a standalone CPython 3.14 on the box

`uv` manages a standalone build. It does **not** touch `/usr/bin/python3`,
which Ubuntu's own tooling needs at 3.10, and it adds no third-party apt
source.

```bash
ssh -i "$KEY" $BOX 'curl -LsSf https://astral.sh/uv/install.sh | sh'
ssh -i "$KEY" $BOX '~/.local/bin/uv python install 3.14'
ssh -i "$KEY" $BOX '~/.local/bin/uv python find 3.14'   # note the path
```

Nothing live has changed at this point.

## Step 3 — build the new venv ALONGSIDE the live one

`--seed` is **load-bearing**: `uv venv` does not install `pip`, and
`deploy.sh` line 58 runs `"${VENV}/bin/pip" install -e "${REPO_DIR}[ui]"`
whenever `pyproject.toml` changes. A venv without pip works perfectly until
the next packaging change, then fails a deploy for a reason that looks
nothing like its cause.

```bash
ssh -i "$KEY" $BOX '
  cd /opt/cfdinvest &&
  ~/.local/bin/uv venv --python 3.14 --seed .venv-3.14 &&
  .venv-3.14/bin/pip install -e ".[ui,dev]" &&
  .venv-3.14/bin/python --version &&
  test -x .venv-3.14/bin/pip && echo "pip present (deploy.sh needs it)" &&
  .venv-3.14/bin/pip list | grep -iE "numpy|pandas|pyarrow|streamlit|yfinance"
'
```

`[dev]` brings pytest, which Step 4 needs. The live `.venv` is untouched and
both services keep running.

## Step 4 — run the full suite on the box, against the new venv

This is the step that earns the whole exercise: the suite has never run on
the machine that runs the account.

```bash
ssh -i "$KEY" $BOX '
  cd /opt/cfdinvest && set -a && . /etc/cfdinvest/env && set +a &&
  .venv-3.14/bin/python -m pytest -q 2>&1 | tail -20
'
```

Then the behavioural preflight — the config the daemon builds, and the
universe decision against the real recorded balance:

```bash
ssh -i "$KEY" $BOX '
  cd /opt/cfdinvest && set -a && . /etc/cfdinvest/env && set +a &&
  .venv-3.14/bin/python -c "
import warnings; warnings.filterwarnings(\"ignore\")
from cfdinvest.config import load_config
from cfdinvest.schedule.app import _execution_config
from cfdinvest.universe.traded import explain_target
s = load_config(); st = s.strategy
d = explain_target(s, len(s.market.tickers or ()))
e = _execution_config(tuple(s.market.tickers or ()), st)
print(\"names held  :\", d.slots)
print(\"bound by    :\", d.bound_by)
print(\"gate        :\", st.entry_confidence)
print(\"opens/pass  :\", st.max_opens_per_pass)
print(\"open delay  :\", st.open_delay_minutes)
print(\"min_deal_sizes      :\", e.min_deal_sizes)
print(\"leverage_cap_selects:\", e.leverage_cap_selects)
assert d.slots == 25, d.slots
assert e.min_deal_sizes is None and e.leverage_cap_selects is False
print(\"OK\")
"
'
```

**Expected**, unchanged from 2026-09-08: 25 names, bound by
`universe.max_target (25)`, gate 0.75, 3 opens per pass, 30-minute open
delay, both new engine fields inert.

> **If anything here fails, stop and delete `.venv-3.14`.** Nothing live has
> been touched and there is no rollback to perform. That is the point of
> building it alongside.

## Step 5 — push the pin commit

Now, and not before. The webhook installs into whichever venv is live, so
the ordering matters: pins land in the *old* venv here, which is harmless
because they match what it already has.

```bash
git push origin main
ssh -i "$KEY" $BOX 'journalctl -u cfdinvest-deploy --since "3 minutes ago" --no-pager | tail -5'
```

## Step 6 — the swap

The only step with downtime. Roughly ten seconds.

```bash
ssh -i "$KEY" $BOX '
  systemctl stop cfdinvest cfdinvest-dashboard &&
  cd /opt/cfdinvest &&
  mv .venv .venv-3.11-rc1-old &&
  mv .venv-3.14 .venv &&
  chown -R cfdinvest:cfdinvest .venv &&
  systemctl start cfdinvest cfdinvest-dashboard &&
  sleep 5 && systemctl is-active cfdinvest cfdinvest-dashboard
'
```

`chown` matters: the old venv is owned by `cfdinvest` and the new one is
built as root. A venv the service user cannot execute fails in a way that
reads like a Python problem.

## Step 7 — verify

```bash
ssh -i "$KEY" $BOX '
  /opt/cfdinvest/.venv/bin/python --version
  systemctl is-active cfdinvest cfdinvest-dashboard
  curl -s -o /dev/null -w "dashboard HTTP %{http_code}\n" http://127.0.0.1:8502
  cd /opt/cfdinvest && set -a && . /etc/cfdinvest/env && set +a &&
    .venv/bin/cfdinvest-schedule monitor 2>&1 | tail -15
'
```

- [ ] `Python 3.14.x`
- [ ] `/opt/cfdinvest/.venv/bin/pip` exists and runs — **deploy.sh calls it**,
      and its absence would surface only at some later deploy
- [ ] both services `active`
- [ ] dashboard HTTP 200 on **8502** (Caddy fronts it; 8501 is not in use)
- [ ] monitor reports no new failures
- [ ] the Universe page still reads **25 names, bound by
      `universe.max_target (25)`**
- [ ] first dealing pass of the next session places or refuses as expected —
      read `persist.orders`, not just the logs

## Rollback

Ten seconds, any time before the old venv is deleted:

```bash
ssh -i "$KEY" $BOX '
  systemctl stop cfdinvest cfdinvest-dashboard &&
  cd /opt/cfdinvest &&
  mv .venv .venv-3.14-failed &&
  mv .venv-3.11-rc1-old .venv &&
  systemctl start cfdinvest cfdinvest-dashboard
'
```

**Keep `.venv-3.11-rc1-old` for at least one full trading week.** It costs
523 MB and it is the only thing that makes this reversible after the fact.
Delete it only once a week of sessions has run clean.

---

## Afterwards

- [ ] Update `deployment/README.md` §1, which says the venv is built from
      the system Python — it will no longer be true
- [ ] Record the change and its date in `CLAUDE.md`
- [ ] Schedule the 22.04 → 24.04 upgrade separately. It is now decoupled: the
      venv no longer depends on any system Python, so the upgrade cannot
      strand it. Standard support ends **April 2027**

## What this does not do

It does not change what the account trades. Every parameter, gate and cap is
untouched, and Step 4 asserts that before anything is swapped. If the account
behaves differently afterwards, that is a bug in this runbook rather than a
strategy change, and the rollback above is the right first move.
