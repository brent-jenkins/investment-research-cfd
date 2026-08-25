# systemd units

```bash
sudo cp deployment/systemd/*.service deployment/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# The scheduler replaces the two timers that came before it.
sudo systemctl disable --now cfdinvest-pipeline.timer cfdinvest-monitor.timer
sudo systemctl enable --now cfdinvest.service
sudo systemctl enable --now cfdinvest.timer      # watchdog; see below

journalctl -u cfdinvest -f
```

| Unit | What it is |
| --- | --- |
| `cfdinvest.service` | The scheduler. One loop, every scheduled job. |
| `cfdinvest.timer` | A watchdog for the above. Not the schedule. |
| `cfdinvest-dashboard.service` | Streamlit, behind Caddy. Untouched. |
| `cfdinvest-pipeline.{service,timer}` | **Superseded.** Disable. |
| `cfdinvest-monitor.{service,timer}` | **Superseded.** Disable. |

---

## Why a daemon rather than more timers

**Because the jobs are ordered.** The pipeline reads what ingestion wrote,
and the monitoring and risk snapshots describe the run that has just
finished. Expressing "five minutes after ingestion, unless ingestion failed"
as separate timers means encoding that ordering in wall-clock guesses and
hoping the first job finished. Here it is a sequence, and a failed ingestion
is a reason not to run the pipeline rather than a race nobody sees.

The five-minute quote poll settles it on its own: a timer firing 288 times a
day to start a Python process that reads a handful of prices is mostly
interpreter startup.

**Only one scheduler may run.** `cfdinvest.service` declares `Conflicts=`
against the two units it replaces, so systemd refuses to run them together
rather than trusting whoever deploys to have read this file. Two schedulers
writing one lake on different schedules is the failure that guard exists for.

Note the schedule moves: the old pipeline timer ran **Mon–Fri 21:30 UTC**,
after the US close. The daemon runs ingestion at **03:00 UTC daily** and the
pipeline at **03:05**.

## Why the timer is a watchdog and not the schedule

`Restart=always` covers an ordinary crash. It does not cover the start
limit — five failures in five minutes and systemd gives up *permanently*.
That limit is deliberate: a daemon crash-looping against a vendor endpoint is
worse than a stopped one, because it looks like it is working. The timer is
what recovers the scheduler afterwards. During normal operation it is a
no-op, because starting a running unit does nothing.

## Why ingestion is daily

The lake holds daily bars. Signals, regimes, correlations and every backtest
are computed on them, so there is nothing a second ingestion in the same day
would change — the bar it would rewrite is the one still forming.

03:00 UTC is after every venue in the universe has settled the previous
session and long before anyone is deciding what to do about it.

## Why live quotes are polled every five minutes

Between one close and the next, every price the helper reasons about is
yesterday's. That is right for a backtest and wrong for someone about to
place a trade.

Five minutes is a compromise: frequent enough that a price is current when
you look at it, infrequent enough that a handful of instruments is a
negligible load on someone else's endpoint. **Only open positions are
polled** — the confirm layer decides which, and an empty book polls nothing.

Quotes are not bars. Only the current price is fetched, only the latest is
kept, and nothing intraday is written to the lake. See
`cfdinvest/livequotes/README.md`.

## Why no broker API calls

Trading 212 has no public price API, and this project makes no broker calls
of any kind — no endpoint, no UI automation, no order path, in any unit here.
That constraint is older than the scheduler and is not relaxed by it.

**Nothing in these units can place a trade.** The daemon computes, records
and notifies. You read the recommendation and place the trade yourself in the
Trading 212 app.

The only outbound calls any of this makes are: the market data vendor and
FRED during ingestion, one public quote endpoint during the poll, and SMTP
when something needs saying.

---

## Hardening

Both scheduler units run with `ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges` and a restricted address family set, writing only to
`/var/lib/cfdinvest`. `/opt/cfdinvest` is read-only to them: the code is
deployed, not modified by what it runs.

`cfdinvest.service` binds no port. The dashboard is a separate unit, and the
`--server.address 127.0.0.1` in it is load-bearing — without it Streamlit
binds every interface and answers past Caddy's basic auth.

Secrets live in `/etc/cfdinvest/env`, `0600` and owned by root, and are never
in a unit file. An `ExecStart` is visible in `ps` to every user on the box.
