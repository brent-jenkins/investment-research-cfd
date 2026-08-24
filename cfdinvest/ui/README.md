# cfdinvest dashboard

A read-only Streamlit view of the trading system: what the lake holds, what
the scheduler did, what the model wants, and what it would cost to do it.

```bash
streamlit run cfdinvest/ui/app.py
```

---

## The read-only contract

**This app cannot trade, cannot write, and cannot fetch.** Every read goes
through [`sources.py`](sources.py), which has no write path at all. There is
no order placement, no config mutation, no data refresh, and no call to any
external service — the market calendar is *computed*, not queried.

That is not a promise in a docstring; it is a test. `tests/test_ui.py`
parses every module in this package and fails if any of them calls a write,
a pipeline entry point or a network verb, or imports `requests`, `yfinance`,
`smtplib` or `subprocess`. A page added in six months without reading this
paragraph will still be caught.

The trades on the Execution page are **recommendations**. They are placed by
hand. The gap between what is listed there and what is in the account is the
difference between the backtest and reality — including any resting stop
order, which only exists if you placed it.

---

## Layout

```
cfdinvest/ui/
    app.py            landing page; the multipage entry point
    theme.py          the palette, and the CSS that applies it
    sources.py        every read; the safety contract lives here
    health.py         the critical-warning checks
    components.py     page setup, the banner, formatting
    configview.py     flattening config, spotting overrides and unknown keys
    pages/
        01_status.py … 07_config.py
```

`sources.py`, `health.py`, `components.py` and `configview.py` are not in the
original outline. They exist because seven pages otherwise duplicate the same
reads, the same banner and the same "this is missing, and here is why" panel
— and because none of that logic is testable while it lives inside a
Streamlit script.

Nothing here imports `streamlit` at module scope except the pages themselves:
the `render_*` helpers take the module as an argument. That is what lets the
whole package be imported, and tested, without a Streamlit runtime.

---

## Data sources — and what does not exist

The pipeline persists less than a dashboard would like. Rather than invent
values, the helpers that have nothing to read say so, and the pages render
the reason instead of a blank panel.

| What the UI shows | Where it actually comes from |
| --- | --- |
| Lake metadata | `DataRepository.coverage()`, `.macro_coverage()`, `.anomalies()`, `.publication_calendar()` |
| Last pipeline run | `<data_root>/runs/<run_id>/summary.json` |
| Signals | `runs/<run_id>/signals.parquet` (long form, active only) |
| Trades, positions, equity | `runs/<run_id>/{trades,positions,equity}.parquet` |
| Market sessions | `schedule.market_calendar.describe()` — computed, offline |
| Scheduler state | **derived from the run directory** — see below |
| Logs | `logging.log_file`, and conventional names beside it or under `<data_root>/logs` |
| Config | `config.toml` + `CFD_*` environment variables |

Four things are genuinely unavailable, and every one of them is surfaced in
the UI rather than hidden:

- **The scheduler keeps no state file.** "Last run" is read from the run
  directory, so it reports when a run last *completed* — it cannot say
  whether a scheduler process is alive right now. A dead scheduler shows up
  as a last run that keeps getting older, which is what the staleness check
  is for. "Exit code" is derived from the run's recorded status; the real
  process exit code is not stored.
- **Regimes are not persisted.** They are computed in memory during a run and
  discarded. The signal `reason` string names the regime that produced each
  decision, so the context survives even though the panel does not.
- **The analysis frame is not persisted.** Prices are readable from the lake;
  the derived panels (returns, volatility, ATR, correlations) are not.
- **The per-bar P&L decomposition is not persisted.** The P&L page
  reconstructs the cost series by summing the trade log per day — exact for
  costs, which are only ever charged on a trade — and takes the run totals
  from `summary.json`. The gap and intraday legs are not recoverable.

Two of these would close by persisting a little more per run: writing
`execution.pnl` and `execution.metadata` alongside the existing artefacts
would give the P&L page its real decomposition and the Config page the
settings the run actually used, rather than the built-in defaults. That is a
change to the scheduler, which this work was explicitly not to touch.

---

## Critical warnings

A persistent red banner appears at the top of **every** page when any
critical check fires:

> ⚠ Critical system warning — results may be unreliable. See Status page.

The failure this exists to catch is a system that *looks* fine. A stale lake
still serves prices. A dead scheduler still leaves yesterday's run sitting
there. A failed FX conversion still produces a number. Every page will render
perfectly against data that is quietly wrong.

| Check | Fires when |
| --- | --- |
| Config readable | `config.toml` cannot be loaded |
| Lake directory readable | the lake is missing or permission-denied |
| Lake freshness | newest observation older than `lake_stale_days` (3) |
| Scheduler activity | last completed run older than `scheduler_stale_hours` (48) |
| Scheduled run not missed | a due event passed the grace window uncompleted |
| Last pipeline run succeeded | the run recorded `status="failed"` |
| Ingestion succeeded | a manifest entry is not `ok`, or the run error names ingestion |
| Execution engine clean | the run error or the log names an execution failure |
| Stop / TP fills computed | a level exit recorded no fill price |
| FX conversion succeeded | a trade has a missing or non-positive FX rate |
| Disk space | below `disk_free_gb` (1) or `disk_free_fraction` (5%) |
| Publication calendar freshness | *warning only* — macro calendar older than 45 days |

Each check states **what was found**, with the numbers in it, and **what to
do**. "Lake is stale" is not actionable; "newest data is 6.2 days old
(threshold 3.0)" is.

A check that raises reports *itself* as failed rather than propagating. A
check that crashes is a check that has silently stopped protecting you.

Thresholds live in `health.HealthThresholds` and are all keyword arguments,
so a deployment that runs weekly rather than daily can widen them without
editing the checks.

---

## Pages

| Page | Shows |
| --- | --- |
| **Status** | Lake freshness, series and anomaly counts, vintage mode, coverage tables; scheduler last/next/duration/exit; market sessions and holidays; the last 10 error lines; every health check expanded |
| **Signals** | Per instrument: direction, size, confidence, reason, stop and take-profit distance (absolute and %), timestamp; full signal history |
| **Execution** | Per instrument: recommended trade and fill, stop/TP levels, notional; spread, slippage, FX fee and rate; gap-through flags; per-bar leverage |
| **Positions** | Per open position: entry, stop, TP, unrealised P&L, financing accrued, FX costs, exposure, leverage; book totals |
| **P&L** | Equity curve and drawdown; realised P&L, spread, slippage, financing and FX totals; daily and cumulative cost charts; daily summary and trade history |
| **Logs** | Errors, scheduler, pipeline, ingestion, execution, and the run-summary history; tail length and substring filter |
| **Config** | Global config from disk, per-instrument settings, and the stop/FX/execution/scheduler/run configs, each with overrides, unset values and unrecognised keys called out |

**Credentials are redacted** on the Config page. Anything whose name looks
like a key, token, secret or password renders as `<redacted, N chars>` — this
page draws into a browser, and a printed key is a key leaked to every proxy
log between the server and the viewer.

---

## Theme

The palette matches example.com and is defined once, in `theme.py`:

| Role | Colour |
| --- | --- |
| background | `#0B0C10` |
| panel | `#1F2833` |
| text | `#C5C6C7` |
| accent | `#66FCF1` |
| accent2 | `#45A29E` |

Applied by injecting a stylesheet with `st.markdown`, because Streamlit's own
theming covers the chrome but not the widgets this app leans on.

Status colours (`#FF4B4B` critical, `#F5A623` warning) are deliberately
**not** drawn from those five. A warning that shares a hue with the accent is
a warning nobody sees.

---

## Part 5 — Local development

```bash
pip install streamlit
streamlit run cfdinvest/ui/app.py
```

Opens on <http://localhost:8501>. No Caddy, no TLS, no auth — bind to
localhost and it is reachable only from the machine it runs on.

`cfdinvest` does not need to be installed: each page puts the repository root
on `sys.path` before importing anything, because Streamlit runs pages as
scripts from their own directory.

### Environment

| Variable | Effect |
| --- | --- |
| `CFD_DATA_ROOT` | Where the lake lives. Everything else derives from it |
| `CFD_LOG_FILE` | Enables file logging, and gives the Logs page something to read |
| `CFD_LOG_LEVEL` | `DEBUG` for verbose pipeline logs |
| `CFD_INTERVAL` | Bar interval, which selects the market manifest |

```bash
CFD_DATA_ROOT=/srv/cfdinvest/data \
CFD_LOG_FILE=/var/log/cfdinvest/cfdinvest.log \
streamlit run cfdinvest/ui/app.py
```

The Logs page finds files by convention: whatever `CFD_LOG_FILE` points at,
plus `scheduler.log`, `error.log`, `ingestion.log` and `execution.log` beside
it or under `<data_root>/logs`. With no dedicated file it filters the
application log by level and by module, and says that it is doing so.

### Debugging

```bash
CFD_LOG_LEVEL=DEBUG streamlit run cfdinvest/ui/app.py --logger.level=debug
```

Reads are cached for 30 seconds so that clicking a widget does not re-read
the lake. The sidebar **Refresh** button clears the cache; during development
`--server.runOnSave true` reruns on every edit.

### Running with no data

Every page is tested against three states: a populated lake, an empty one,
and no lake at all. A fresh checkout with no `data/` directory renders
correctly — the banner explains what is missing rather than the app throwing
a traceback at you.

---

## Part 6 — VM deployment behind Caddy

Streamlit binds to localhost, Caddy terminates TLS and proxies to it. The
dashboard is never exposed directly.

### 1. Streamlit config

`/srv/cfdinvest/.streamlit/config.toml`:

```toml
[server]
address = "127.0.0.1"          # never 0.0.0.0 — Caddy is the only client
port = 8501
headless = true
enableCORS = false             # TLS is terminated by Caddy
enableXsrfProtection = true
runOnSave = false

[browser]
serverAddress = "cfd.example.com"
gatherUsageStats = false

[theme]
base = "dark"
backgroundColor = "#0B0C10"
secondaryBackgroundColor = "#1F2833"
textColor = "#C5C6C7"
primaryColor = "#66FCF1"
```

The `[theme]` block styles the chrome Streamlit draws before any page code
runs, so the app does not flash white on load. `theme.py` still does the rest.

### 2. systemd unit

`/etc/systemd/system/cfdinvest-ui.service`:

```ini
[Unit]
Description=cfdinvest read-only dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cfdinvest
Group=cfdinvest
WorkingDirectory=/srv/cfdinvest
Environment="CFD_DATA_ROOT=/srv/cfdinvest/data"
Environment="CFD_LOG_FILE=/var/log/cfdinvest/cfdinvest.log"
ExecStart=/srv/cfdinvest/.venv/bin/streamlit run cfdinvest/ui/app.py
Restart=on-failure
RestartSec=5

# The dashboard only ever reads. Let systemd enforce that too.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadOnlyPaths=/srv/cfdinvest
ReadWritePaths=/srv/cfdinvest/.streamlit

[Install]
WantedBy=multi-user.target
```

`ReadOnlyPaths` is the belt to the code's braces: even a bug could not write
to the lake. `.streamlit` stays writable because Streamlit keeps a cache
there.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cfdinvest-ui
sudo systemctl status cfdinvest-ui
```

### 3. Caddy

`/etc/caddy/Caddyfile`:

```caddy
cfd.example.com {
    encode zstd gzip

    # HTTPS is automatic: Caddy provisions and renews a Let's Encrypt
    # certificate as long as the A record points here and 80/443 are open.

    reverse_proxy 127.0.0.1:8501 {
        header_up X-Real-IP {remote_host}
        # Streamlit is a websocket app; Caddy upgrades /_stcore/stream itself,
        # but the read timeout has to outlast an idle browser tab.
        transport http {
            read_timeout 86400s
        }
    }
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

### 4. Optional — basic auth

```bash
caddy hash-password --plaintext 'your-password'
```

```caddy
cfd.example.com {
    basic_auth {
        brent $2a$14$...the-hash-from-above...
    }
    reverse_proxy 127.0.0.1:8501 {
        transport http {
            read_timeout 86400s
        }
    }
}
```

Basic auth over HTTPS is fine for one operator. It is not a session system:
the browser sends the credentials on every request, and there is no logout.

### 5. Optional — IP allowlist

Cheaper and stronger than a password if the addresses are stable:

```caddy
cfd.example.com {
    @allowed remote_ip 203.0.113.4 198.51.100.0/24
    handle @allowed {
        reverse_proxy 127.0.0.1:8501 {
            transport http {
                read_timeout 86400s
            }
        }
    }
    handle {
        respond "Not found" 404
    }
}
```

Returning 404 rather than 403 means a scanner learns nothing about what is
here. Combine with basic auth by putting both inside `handle @allowed`.

Also close the port at the firewall — Caddy is the only thing that should
reach Streamlit:

```bash
sudo ufw allow 80,443/tcp
sudo ufw deny 8501/tcp
```

### 6. Checks after deploying

```bash
curl -sI https://cfd.example.com | head -1        # 200, or 401 with auth
curl -sI http://<server-ip>:8501 | head -1        # should fail: not exposed
journalctl -u cfdinvest-ui -n 50 --no-pager
```

Then open the dashboard and confirm the Status page agrees with the server:
if the lake path is wrong, the banner says so on the first screen rather than
showing you an empty table.

---

## Testing

```bash
pytest tests/test_ui.py -q      # 115 tests, no network
```

The pages are executed by Streamlit's own `AppTest` runner — against a real
runtime, not a stub — over three states: a populated synthetic lake, an empty
lake, and no lake at all. That is what catches the failure that actually
happens here, which is an argument Streamlit no longer accepts.

A hand-written stub was tried first. It passed everything, including a
`st.dataframe(height=None)` call that the real runtime rejects on every page
that used it. A permissive stub tests the stub.
