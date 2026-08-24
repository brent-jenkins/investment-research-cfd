# Scheduling and orchestration

Runs the pipeline automatically, market-aware, with optional email. Works
the same locally and on an unattended VM.

Nothing here models markets. It decides *when* the pipeline runs and *who
hears about it*; the pipeline itself is `cfdinvest.backtest`.

```bash
python -m cfdinvest.schedule.app market-status          # what's open right now
python -m cfdinvest.schedule.app run-once               # run and exit
python -m cfdinvest.schedule.app run-continuous         # wait for market events
python -m cfdinvest.schedule.app send-test-email --dry-run
python -m cfdinvest.schedule.app crontab                # print crontab lines
```

**Exit codes** matter unattended — cron mails on non-zero, and a monitor can
alert on it: `0` ran, `1` failed, `2` bad arguments or configuration.

---

## Local mode

```bash
# See what's open. Useful first, because run-once skips closed markets.
python -m cfdinvest.schedule.app --tickers "SPY,TLT,GLD" market-status

# Run against whatever is in the lake, no email.
python -m cfdinvest.schedule.app --tickers "SPY,TLT,GLD" --no-email run-once --force

# Preview the emails without sending anything.
python -m cfdinvest.schedule.app --dry-run run-once --force
```

`--force` is needed outside market hours: by default a run **skips when
every market in the universe is closed** and prints the session table
instead, so a cron entry that fires on a holiday does nothing rather than
reprocessing yesterday.

`--tickers` is **comma-separated**, not a space-separated list. A greedy
list option placed before a subcommand swallows the subcommand — 
`--tickers SPY TLT run-once` fails with "command required" — and this also
matches the `CFD_TICKERS` environment convention used elsewhere.

### What a run leaves behind

Each run writes to `<data_root>/runs/<run_id>/`:

```
20260304T211500Z/
├── summary.json      run metadata, metrics, latest signals and trades
├── equity.parquet    the simulated account value
├── trades.parquet    the trade log
├── positions.parquet units held per bar
└── signals.parquet   active signals only
```

`keep_runs` (default 30) prunes the oldest. Without a cap an unattended VM
fills its disk after a few months, and that failure surfaces as something
apparently unrelated.

---

## Market calendar

Static session rules per asset class — no vendor calendar package, no
network.

| Class | Session | Zone |
| --- | --- | --- |
| `equity` | 09:30–16:00 | America/New_York |
| `equity_eu` | 08:00–16:30 | Europe/London |
| `equity_jp` | 09:00–15:00 | Asia/Tokyo |
| `bond`, `commodity` | 08:00/09:00–17:00 | America/New_York |
| `fx` | Sun 17:00 → Fri 17:00, continuous | America/New_York |
| `volatility` | 09:30–16:15 | America/New_York |
| `crypto` | 24/7 | — |

**Holidays are computed from rules, not listed.** A hardcoded table works
until the year after its last entry, then silently reports a holiday as a
trading day — which on an unattended VM means a run that quietly does
nothing. Third Monday in January, last Monday in May, Good Friday via the
Gregorian Easter algorithm, weekend observation shifts, Juneteenth from
2022. Tested against known dates through 2035.

**Sessions are stored as local times in a named zone**, so daylight saving
is handled: the US equity open is 09:30 local all year, which is 14:30 UTC
in winter and 13:30 UTC in summer.

### Known gaps

- **Early closes are not modelled.** Half-days before Thanksgiving and
  Christmas close at 13:00 ET; this reports 16:00, so a close-triggered run
  fires three hours late on those days.
- **Non-US exchange holidays** are approximated by weekends only.
- **FX observes no US holidays** — technically open, though thin.

---

## VM deployment

### Recommended: cron + `run-once`

The process does its work and exits. Nothing to supervise, nothing that
leaks over a month, and the OS restarts it after a reboot without help.

```bash
python -m cfdinvest.schedule.app --tickers "SPY,TLT,GLD,EURUSD=X" crontab
```

which prints, ready to paste:

```cron
# cfdinvest scheduled runs.
# Times are LOCAL TO EACH SESSION's timezone -- set CRON_TZ per block,
# or the schedule drifts by an hour at every daylight-saving change.

# close + 15min for: SPY
CRON_TZ=America/New_York
15 16 * * 1-5 cfdinvest-schedule run-once

# close + 15min for: EURUSD=X, GLD
CRON_TZ=America/New_York
15 17 * * 1-5 cfdinvest-schedule run-once
```

`CRON_TZ` matters. A US equity close pinned to a fixed UTC hour is an hour
wrong for half the year.

Runs default to **15 minutes after the close**, giving the vendor time to
publish the closing bar. Running at the *open* would act on yesterday's data
carrying today's timestamp, which is why `on_close` is the default.

A full crontab for a VM:

```cron
CRON_TZ=America/New_York
15 16 * * 1-5 cd /opt/cfdinvest && /opt/cfdinvest/.venv/bin/python -m cfdinvest.schedule.app --tickers "SPY,TLT,GLD" run-once --refresh >> /var/log/cfdinvest.log 2>&1
```

`--refresh` tops the lake up from the vendors first. It is **off by
default**, so the documented behaviour is "read the lake" — turn it on when
nothing else keeps the data current.

### Alternative: a long-lived process

For hosts without cron, or when a run must fire promptly after an event
rather than at a fixed wall-clock time:

```bash
python -m cfdinvest.schedule.app --tickers "SPY,TLT" run-continuous
python -m cfdinvest.schedule.app --tickers "SPY,TLT" run-continuous --apscheduler
```

`--apscheduler` uses APScheduler's cron triggers, which handle DST per
timezone properly and give a misfire grace period, so a run skipped during a
restart still fires rather than vanishing. Without the flag, a plain sleep
loop does the same job with no dependency.

Under systemd:

```ini
[Unit]
Description=cfdinvest scheduler
After=network-online.target

[Service]
Type=simple
User=cfdinvest
WorkingDirectory=/opt/cfdinvest
EnvironmentFile=/etc/cfdinvest/env
ExecStart=/opt/cfdinvest/.venv/bin/python -m cfdinvest.schedule.app --tickers "SPY,TLT,GLD" run-continuous
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Put the SMTP credentials in `/etc/cfdinvest/env` with mode `0600`, not in
the unit file — a unit file is world-readable.

---

## Email setup

Credentials come from the **environment**, never from a config file that
could be committed:

```bash
export CFD_SMTP_HOST=smtp.gmail.com
export CFD_SMTP_PORT=587
export CFD_SMTP_USER=you@gmail.com
export CFD_SMTP_PASSWORD='your-app-password'   # not your account password
export CFD_SMTP_SENDER=you@gmail.com
export CFD_SMTP_RECIPIENTS='you@gmail.com,someone@else.com'
export CFD_SMTP_USE_TLS=true
export CFD_SMTP_ENABLED=true
```

Then prove the path works before relying on it:

```bash
python -m cfdinvest.schedule.app send-test-email
```

It reports exactly what is missing rather than failing obscurely, and exits
`2` on a configuration problem.

For Gmail you need an **app password** with 2FA enabled; the normal account
password will be rejected. Any SMTP server works — the module has no
provider-specific logic.

### Three notification types

| Function | Sent when |
| --- | --- |
| `send_trade_alert(trade)` | One per trade on the latest bar (`run-once --trade-alerts`) |
| `send_daily_summary(summary)` | After every successful run |
| `send_error_alert(error)` | When a run fails |

**A send failure never propagates.** A run that did its work must not be
reported as broken because a mail server was briefly unreachable — the
notification is a courtesy, not the work. Failures are logged and swallowed.

**The password never appears in a repr or log line** from this module.

Message *building* is separated from *sending*, and both from the transport.
The builders are pure: given a summary they return an `EmailMessage`, so
they are tested by reading the text with no server and no mocking.

---

## Testing

```bash
pytest tests/test_schedule_*.py     # 110 tests, no network, no SMTP
```

| File | Covers |
| --- | --- |
| `test_schedule_calendar.py` | Sessions, holiday rules through 2035, DST, the FX week |
| `test_schedule_orchestration.py` | Runner, notifications, scheduler, CLI |

The scheduler tests **inject a fake clock and sleeper**, so a week of market
events is simulated in milliseconds and the assertions are about which
events fired rather than about elapsed time. Emails are captured by
`RecordingTransport`, so the full notification path runs offline.

Two bugs the tests caught during the build, both fixed and pinned:

- **`--tickers` swallowed the subcommand.** A greedy `nargs="*"` option
  before a subparser consumes it, so `market-status` was parsed as a ticker
  and argparse reported "command required".
- **`crontab --command` overwrote which subcommand ran.** The subparsers use
  `dest="command"`, so a plain `--command` on one of them clobbered it. It
  now uses an explicit `dest="cron_command"`.

A third, pre-existing, surfaced here: `test_config.py` leaked
`CFD_INTERVAL=1h` into `os.environ` via `load_dotenv`, which steered any
later test that read the environment. Closed.

---

## Limitations

**This schedules a simulation.** No orders reach any broker. Every email
says so explicitly, because a daily message listing trades is exactly the
kind of thing that gets misread six months later.

**No run-level locking.** Two overlapping cron entries would both run and
both write. Use a single schedule, or wrap the command in `flock`.

**No retry.** A failed run alerts and waits for the next trigger. For a
daily strategy that is the right behaviour; a faster cadence would want
backoff.

**The calendar is static.** Unscheduled closures — an exchange outage, a
national day of mourning — are not known to it, and a run on such a day
processes whatever the vendor returned.
