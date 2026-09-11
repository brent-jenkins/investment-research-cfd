# Operational monitoring

Is the machine alright — on a box nobody is watching?

```bash
cfdinvest-schedule monitor
cfdinvest-schedule monitor --watch --interval-minutes 30 --alert
```

```python
from cfdinvest.monitoring import run_monitoring

snapshot = run_monitoring(config)
print(snapshot.overall_status, snapshot.headline)
```

Also a page in the dashboard: **Operational Monitoring**, and a row on
**System status**.

---

## The failure this exists to catch

**A system that looks fine.**

A stale lake still serves prices. A dead scheduler still leaves yesterday's
run sitting there. A half-written run directory looks exactly like a whole one
from the outside. Every page in the dashboard will render perfectly against
data that is quietly wrong, and none of these announce themselves.

Twelve checks, run after every pipeline run:

| Check | The failure it catches |
| --- | --- |
| Lake freshness | Signals computed on prices from last month |
| Scheduler health | A run that started and never came back |
| Disk space | The thing that makes every other failure confusing |
| Run failures | A run that recorded an error nobody read |
| Missing artefacts | Half a run, rendering as a whole one |
| Confirmation drift | The book you hold differing from the book being scored |
| Email delivery | Alerts that stopped arriving, silently |
| Pipeline anomalies | A NaN equity, a wiped-out account, zero bars |
| System resources | A box under strain |
| Dealing liveness | The market open, the unit `active`, and no order placed |
| Credential expiry | A broker session that will stop working on a known date |
| Live quote freshness | A quote feed still answering, with stale numbers |

**Run this one OUT OF PROCESS or it cannot work.** `check_dealing_liveness`
catches a daemon that is alive and wedged -- the process up, the unit
`active`, `Restart=always` never firing. A check running inside that daemon's
own loop stops when the daemon stops, so it is exactly the failure it cannot
report on. `cfdinvest-monitor.service` runs on its own thirty-minute timer
for that reason. It was written after the 2026-09-09 stall, scheduled only
inside the daemon at 03:05 UTC where `market_open` is always False, and so
was silent through a second nineteen-hour stall on 2026-09-11.

---

## The rule the whole layer turns on

**A check that could not run is not a passing check.**

An unanswerable question about safety is not a reassuring answer. So "the lake
metadata could not be read" is a warning in its own right, never an absence of
issues, and the page shows every check including the ones that passed —
a summary of only what broke cannot tell you what was looked at.

```python
check_lake_freshness(None)   # -> [warning: "Lake freshness could not be checked"]
```

The same distinction runs through everything else here:

- Nothing confirmed is **not** a confirmation mismatch. Silence leaves the
  simulated positions standing, and reporting it would make every unattended
  week look broken.
- CPU and memory absent is **not** CPU and memory fine. `psutil` is not a
  dependency of this project, and on a box without it these read "not
  measured".
- A run with no signals is **not** a failed run.

---

## Three severities, and only two of them move the colour

```
red      something is critical
yellow   something is a warning, or could not be checked
green    everything was checked and everything was clear
```

Informational issues — psutil missing, mail switched off, three old failures
on an otherwise healthy scheduler — do not move the status. A colour that went
yellow for a note would stop meaning anything, and a status that never means
anything is one nobody looks at.

**Only red sends mail.** An alert that arrives every day for a yellow is an
alert nobody opens on the day it is red.

---

## Every issue carries its remedy

"Disk space low" is not actionable. This is:

```
[CRITICAL] Disk space
  3.1 GB free (2%) — below the 1.0 GB / 5% floor.
  -> A full disk makes every other failure confusing: the lake stops
     updating and runs fail to write, with nothing saying why. Lower
     `keep_runs` to prune old run directories, or resize the volume.
```

The remedy lives on the issue rather than in whatever displays it, so the
email, the page and the log all say the same thing. An alert that names a
problem without a next step is one that gets archived.

The subject line carries the **worst single problem**, not a count. "7 issues"
tells you nothing about whether to get out of bed.

---

## One set of thresholds

`cfdinvest.ui.health` powers the dashboard banner; this powers the unattended
email. They ask different questions of different inputs — "is what this page
is showing you trustworthy?" against "is the box alright?" — but they share
the numbers:

```python
HealthThresholds().lake_stale_days is MonitoringThresholds().lake_stale_days
```

Two copies would drift, and the day they drift is the day the banner says fine
while the email says critical, with nothing to say which to believe. A test
asserts they stay equal.

The scheduler check goes further and delegates outright to
`cfdinvest.risk.scheduler_status`, so the risk dashboard and the monitoring
alert cannot disagree about whether a run died.

---

## It cannot start a run

There is no import of the backtest engine and none of the scheduler's runner.
A monitoring pass that could start a run would be reporting on a system it had
just disturbed.

The repository *is* imported, for one read — the coverage manifest, where the
newest bar in the lake is recorded. That is a read of a written manifest, not
a fetch: it cannot reach a vendor and cannot ingest.

The dashboard is not imported either. It is an optional install, and a
monitoring layer that needed the UI to find out whether the data was fresh
would fail on exactly the machine it exists to watch. Both are asserted by
parsing the imports.

---

## One failing check does not silence the other eight

Which is exactly the case where you most need them. A check that raises is
caught, reported as its own warning, and the pass continues:

```
[WARNING] Disk space
  The disk space check itself failed: division by zero
  -> This is a bug in the monitoring, not necessarily in what it was
     checking — and it means that thing is now unchecked.
```

For the same reason, a monitoring failure cannot fail a completed run. The
layer that reports problems must not become one.

---

## Snapshots

```
monitoring/
    20260825-120000-monitor.json
    20260825-123000-monitor.json
```

Append-only, one file per pass, same shape as the risk snapshots and for the
same reason: **a status that turned red an hour ago is a different problem
from one that has been red for a week**, and only the history can tell them
apart.

The snapshot's own timestamp names the file, so "the latest file" and "the
latest timestamp" cannot be different snapshots. Two passes in the same second
get a counter rather than one landing on the other.

A failed write returns `None` rather than raising — the likeliest reason a
monitoring write fails is the full disk it exists to report, and crashing
would replace the report with a traceback.

Opening the dashboard page runs the checks but **records nothing**. A
dashboard left open on a second monitor would otherwise file a snapshot every
rerun and bury the ones the scheduler took. Pruning is opt-in and never
automatic: the layer that reports a full disk should not also be the layer
that quietly deletes evidence to avoid one.

---

## Where it runs

```python
RunConfig(
    monitoring=True,          # check after each run
    store_monitoring=True,    # keep the history
    monitoring_alerts=True,   # email on red
)
```

The pass runs *after* the scheduler state is recorded, so it sees the run it is
reporting on rather than the one before it.

Mail delivery is tracked by a small in-process journal in
`cfdinvest.schedule.notify`. `send()` swallows failures by design — an
unattended run that did its work must not be reported as broken because a mail
server was unreachable — and that is precisely why the failure has to surface
somewhere else. This is somewhere else.

---

## Layout

| Module | What it does |
| --- | --- |
| `core.py` | The nine checks and the thresholds. Pure, offline, deterministic. |
| `aggregate.py` | Counts, grouping, the colour and the headline. Pure. |
| `collect.py` | Reads the lake and the machine, runs everything, records it. |
| `resources.py` | The only thing that touches the machine. |
| `cfdinvest/persist/monitoring.py` | Snapshots on disk. |

`core.py` stays pure so a test can describe a full disk without having one, a
dead scheduler without killing one, and a NaN equity without producing one.

---

## Tests

```bash
python -m pytest tests/test_monitoring.py -q
```

94 tests. Beyond the thresholds, they pin:

- **"Not checked" never reads as "clear"**, for every check that can fail to run.
- **Information alone stays green.**
- **The headline names the worst problem, not a count.**
- **The scheduler check agrees with the risk dashboard** on the same state.
- **The banner and the alert read the same thresholds.**
- **One failing check does not silence the others.**
- **A monitoring failure does not fail the run.**
- **A pass writes no run** — `list_runs` before and after.
- **The layer needs neither the dashboard nor the engine**, by parsing imports.
- **Reading the machine degrades without psutil** rather than crashing.
- **The filename and the content agree**, and two passes in a second both survive.

The suite was mutation-checked: making an unreadable lake return no issues,
turning yellow on information, restoring the `bars or …` short-circuit that
hid a zero-bar run, letting a crashed check propagate, letting a monitoring
failure fail the run, and alerting on yellow each make it fail.
