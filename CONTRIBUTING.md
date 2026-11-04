# Restarting and extending this project

The project was closed on 2026-11-04. Its main finding is a null result: a
short-horizon trend follower on US large-cap shares had no edge net of
Capital.com's costs. The machinery is sound, the backtest reproduces a live
account's decisions, and the method is written down. If you want to point
it at a new idea, this is where to start.

Read [FINDINGS.md](FINDINGS.md) first. It lists what has already been tried,
so you don't spend a month re-running it.

---

## Getting it running

```bash
pip install -e ".[dev]"
pytest                       # 3,923 tests, offline, no data or keys needed
```

The data is **not** in the repository. You rebuild the data store, which
the code calls the lake, yourself:

- **Daily and hourly prices from yfinance**, plus the macro series from
  FRED, are fetched by `cfdinvest-schedule run-once --refresh`. FRED needs a
  free API key in `FRED_API_KEY`.
- **Capital.com's hourly bars and quoted spreads** are what the shipped
  configuration decides and charges on. They need a Capital.com demo
  account, which is free, and the `CFD_CAPITAL_*` variables.
- **Minute bars** are served for about 180 days only. A day you don't
  collect is gone.

Secrets go in the environment or a git-ignored `.env`, never in the code.
`tests/test_deployment.py` fails the build if anything password-shaped
appears under `deployment/`.

## Rules that keep the results honest

These are not style preferences. Each one is there because breaking it
produced a wrong number that was believed for a while. `CLAUDE.md` tells
each story.

1. **Pre-register.** Before you read any outcome data, write down the
   question, the test, the pass mark and what you will do on each result.
   Most of the later entries in `CLAUDE.md` show the format.
2. **One engine.** Anything that decides a position goes through
   `execution.run_backtest`, with its rules expressed as `ExecutionConfig`
   settings. Don't re-implement a rule in a notebook or a harness. Use a
   scratch overlay to screen an idea, then re-measure it through the
   engine before you believe it.
3. **Anchor to real fills.** `research/replay.py` checks the backtest
   against what a live engine actually decided. If you change the engine,
   check that it still reproduces.
4. **Treat a surprising number as a bug.** The best backtest result this
   project ever produced, +467.7% a year, was a look-ahead. Corrected, it
   was -47.5%.
5. **Mutation-check every test.** Reintroduce the bug a test guards
   against and confirm the test fails. Each package README lists the
   mutations it checked.
6. **Demo before live.** Live dealing needs two separate environment
   variables, worded differently. Leave `strategy.opens_paused = true`
   until a strategy has passed a pre-registered test.

## Before re-enabling any scheduled job

The three sentiment experiments' collection jobs are still in the code,
unrun: `record-headlines`, `record-client-sentiment` and `label-headlines`.
All three were stopped as failures. **Remove them before you switch any
timer back on**, or they will start collecting again with no experiment
registered behind them.

## Where there might still be something

None of these is a strong lead. They are the edges of what was tested,
not results.

- **Order flow over minutes, across all venues.** The order-flow test
  used Nasdaq's feed only, from the first half-hour against the rest of
  the day. The research literature finds order flow strongest over
  minutes, and that was never tested.
- **A cheaper venue.** Futures or direct market access cost a fraction of
  a CFD's spread. That only matters once something predicts direction,
  because the gross return here was zero before any cost.
- **Fading news-backed moves.** When a company's own filing agreed with
  the engine's trend, the rest of the day went against both, in both
  halves of the sample (-0.43% a trade). The test was registered expecting
  the opposite, so this is a lead for a new pre-registration on fresh
  data, not a result.
- **A universe chosen without hindsight.** The shares were picked in 2026.
  A point-in-time universe, such as index members as of each date, would
  remove the survivorship bias from every long-leaning figure.
- **Slower trend following on futures.** Across 21 markets on daily bars,
  trend following was positive before costs, almost all of it in
  2005-2015, and it lost after Capital's carry. A venue without overnight
  CFD financing might change that last step.

## Contributing back

Fork it, and open a pull request if you want to share something. A null
result is welcome as long as it was registered in advance: it saves the
next person the month. Please keep the house style. Each package README
explains the failure that package exists to prevent, comments explain
*why*, and a retracted figure is struck through with its reason rather
than deleted.
