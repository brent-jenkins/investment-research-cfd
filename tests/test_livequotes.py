"""The live quote poller, and everything that decides whether to trust it.

Offline throughout. :func:`cfdinvest.livequotes.sources.fetch_quote` takes an
``opener`` seam so the parsing can be exercised against real response shapes
without a network, and the structural tests below assert that the packages
which *read* a quote cannot fetch one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.livehelper import price_source, t212_instruments
from cfdinvest.livequotes import poller, sources, storage
from cfdinvest.schedule import daemon

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


def config_for(root: Path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=root))


def response(price=426.69, **meta):
    """A chart response shaped like the real one."""
    body = {"regularMarketPrice": price, "currency": "USD",
            "regularMarketTime": 1787662312}
    body.update(meta)
    return {"chart": {"result": [{"meta": body}]}}


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #
def test_a_price_is_read_from_the_meta_block():
    quote = sources.fetch_quote("GLD", opener=lambda url: response())

    assert quote.ok and quote.price == pytest.approx(426.69)
    assert quote.currency == "USD"


def test_the_symbol_is_escaped_into_the_url():
    """``^VIX`` and ``USDJPY=X`` are not URL-safe as they stand."""
    seen = {}

    def opener(url):
        seen["url"] = url
        return response()

    sources.fetch_quote("USDJPY=X", opener=opener)
    assert "USDJPY%3DX" in seen["url"]
    assert "USDJPY=X" not in seen["url"]


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"chart": {}},
        {"chart": {"result": []}},
        {"chart": {"result": [{}]}},
        {"chart": {"result": [{"meta": {}}]}},
        {"chart": {"result": [{"meta": {"regularMarketPrice": None}}]}},
        {"chart": {"result": [{"meta": {"regularMarketPrice": "n/a"}}]}},
    ],
    ids=["empty", "no result key", "no results", "no meta", "meta empty",
         "null price", "unparseable price"],
)
def test_a_missing_field_is_absent_not_zero(document):
    """Every shape of missing must arrive as None, never as a number."""
    quote = sources.fetch_quote("GLD", opener=lambda url: document)

    assert quote.price is None
    assert not quote.ok
    assert quote.error


def test_a_zero_price_is_refused():
    """Nothing here trades at zero, so a zero is a missing field in disguise.

    It matters because a zero would not merely be wrong -- it would size a
    position as though the instrument were free.
    """
    for value in (0, 0.0, -1.5):
        assert sources.fetch_quote("GLD", opener=lambda url: response(value)).price is None


def test_a_failing_request_is_returned_not_raised():
    def boom(url):
        raise TimeoutError("took too long")

    quote = sources.fetch_quote("GLD", opener=boom)
    assert quote.price is None
    assert "TimeoutError" in quote.error


# --------------------------------------------------------------------------- #
# The poller
# --------------------------------------------------------------------------- #
def test_an_instrument_that_could_not_be_priced_stays_in_the_mapping():
    """"Could not read SPY" and "was not asked about SPY" differ."""
    def fetch(symbol):
        return sources.Quote(symbol, 100.0 if symbol == "GLD" else None)

    prices = poller.poll_live_quotes(["GLD", "SLV"], fetch=fetch)

    assert prices == {"GLD": 100.0, "SLV": None}


def test_each_instrument_is_asked_for_once():
    asked = []

    def fetch(symbol):
        asked.append(symbol)
        return sources.Quote(symbol, 1.0)

    poller.poll_live_quotes(["GLD", "GLD", "SLV"], fetch=fetch)
    assert asked == ["GLD", "SLV"]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_an_unreadable_price_is_left_out_rather_than_written_as_null(tmp_path):
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 426.69, "SLV": None}, config, now=NOW)

    document = storage.load_quotes(config)
    assert document["quotes"] == {"GLD": 426.69}
    assert "SLV" not in document["quotes"]
    assert document["unavailable"] == ["SLV"]


def test_a_stale_quote_is_refused_rather_than_used_with_a_caveat(tmp_path):
    """A price that is wrong in a fast market beats one known to be old."""
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 426.69}, config, now=NOW)

    fresh, _ = storage.quote_for("GLD", config, now=NOW + pd.Timedelta(minutes=5))
    stale, reason = storage.quote_for(
        "GLD", config, now=NOW + pd.Timedelta(minutes=30)
    )

    assert fresh == pytest.approx(426.69)
    assert stale is None
    assert "30 minutes old" in reason


def test_a_quote_file_that_cannot_date_itself_is_not_treated_as_fresh(tmp_path):
    """Unknown age is not "fine". Silence is never read as reassurance."""
    config = config_for(tmp_path)
    path = storage.latest_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"quotes": {"GLD": 1.0}}), encoding="utf-8")

    price, reason = storage.quote_for("GLD", config, now=NOW)

    assert price is None
    assert "does not say when" in reason


def test_a_corrupt_quote_file_reads_as_no_quotes(tmp_path):
    config = config_for(tmp_path)
    path = storage.latest_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert storage.load_quotes(config)["quotes"] == {}
    assert storage.quote_for("GLD", config, now=NOW)[0] is None


def test_the_file_holds_only_the_latest_poll(tmp_path):
    """Latest-only by design: an intraday history beside the lake is what
    this feature exists not to build."""
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 1.0}, config, now=NOW)
    storage.write_quotes({"GLD": 2.0}, config, now=NOW + pd.Timedelta(minutes=5))

    assert storage.load_quotes(config)["quotes"] == {"GLD": 2.0}
    assert len(list(storage.livequotes_root(config).glob("*.json"))) == 1


# --------------------------------------------------------------------------- #
# Which price the helper uses
# --------------------------------------------------------------------------- #
def test_what_you_typed_beats_every_stored_price(tmp_path):
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 426.69}, config, now=NOW)

    chosen = price_source.resolve_price(
        "GLD", given=999.0, app_config=config, now=NOW
    )

    assert chosen.price == 999.0
    assert chosen.origin == "given"


def test_a_fresh_quote_is_preferred_and_a_stale_one_is_not(tmp_path):
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 426.69}, config, now=NOW)

    fresh = price_source.resolve_price("GLD", app_config=config, now=NOW)
    stale = price_source.resolve_price(
        "GLD", app_config=config, now=NOW + pd.Timedelta(minutes=30)
    )

    assert fresh.origin == "live" and fresh.is_live
    assert "Using live quote" in fresh.note
    # No lake in tmp_path either, so the fallback finds nothing -- what
    # matters is that it stopped trusting the quote.
    assert stale.origin != "live"
    assert "30 minutes old" in stale.note


def test_the_note_says_which_price_was_used(tmp_path):
    config = config_for(tmp_path)
    storage.write_quotes({"GLD": 1.0}, config, now=NOW)

    assert price_source.resolve_price(
        "GLD", app_config=config, now=NOW
    ).note.startswith("Using live quote")
    assert price_source.resolve_price(
        "NOPE", app_config=config, now=NOW
    ).note.startswith("No price available")


# --------------------------------------------------------------------------- #
# What may import what
# --------------------------------------------------------------------------- #
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    names |= {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    }
    return names


def test_only_one_module_in_this_package_can_reach_the_network():
    """The split this package is built around.

    ``storage`` is imported by the live helper, the risk layer and
    monitoring -- all of which sit behind tests forbidding them a network. If
    reading a quote could pull in the fetcher, those tests would still pass
    and would no longer mean anything.
    """
    root = Path("cfdinvest/livequotes")
    for name in ("storage.py", "poller.py", "__init__.py"):
        imported = _imports(root / name)
        assert not [
            module for module in imported
            if module.split(".")[0] in {"urllib", "http", "socket", "requests"}
        ], f"{name} must not be able to open a socket"

    assert any(m.startswith("urllib") for m in _imports(root / "sources.py"))


def test_the_live_helper_cannot_fetch_a_quote():
    """It may read one. Fetching would let a price block the helper."""
    for name in ("price_source.py", "cli.py", "safety.py", "sizing_helper.py"):
        imported = _imports(Path("cfdinvest/livehelper") / name)
        assert "cfdinvest.livequotes.sources" not in imported, name
        assert not [
            module for module in imported
            if module.split(".")[0] in {"urllib", "requests", "httpx", "socket"}
        ], name


def test_monitoring_and_persistence_cannot_fetch_a_quote():
    for path in (
        Path("cfdinvest/monitoring/collect.py"),
        Path("cfdinvest/monitoring/core.py"),
        Path("cfdinvest/persist/risk.py"),
    ):
        assert "cfdinvest.livequotes.sources" not in _imports(path), path


# --------------------------------------------------------------------------- #
# What the daemon owes, and when
# --------------------------------------------------------------------------- #
def test_nothing_is_owed_twice_in_one_day():
    last = {
        "poll-live-quotes": pd.Timestamp("2026-08-25 03:58", tz="UTC"),
        "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
        "monitoring-snapshot": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
    }
    assert daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), last) == []


def test_a_restart_after_the_daily_slot_does_not_run_it_again():
    """The case an elapsed-time check gets wrong.

    A daemon restarted at 03:30 has missed nothing if it already ran at
    03:00, and "more than an hour since the last run" would say otherwise.
    """
    last = {
        "poll-live-quotes": pd.Timestamp("2026-08-25 03:28", tz="UTC"),
        "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
        "monitoring-snapshot": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
    }
    assert daemon.jobs_due(pd.Timestamp("2026-08-25 03:30", tz="UTC"), last) == []


def test_the_daily_work_comes_round_again_the_next_day():
    last = {
        "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
        "monitoring-snapshot": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
    }
    due = daemon.jobs_due(pd.Timestamp("2026-08-26 03:06", tz="UTC"), last)
    assert "ingest-daily" in due and "monitoring-snapshot" in due


def test_ingestion_is_ordered_before_what_reads_it():
    due = daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), {})
    # The snapshots describe a lake ingestion has finished writing, and the
    # prune must not remove a run before they have described it.
    assert due.index("ingest-daily") < due.index("monitoring-snapshot")
    assert due.index("monitoring-snapshot") < due.index("risk-snapshot")
    assert due.index("risk-snapshot") < due.index("prune-runs")


def test_the_snapshots_are_owed_once_a_day_and_not_again():
    """M222: the snapshots back on the cadence of whatever triggers them.

    They used to hang off the daily pipeline run. That run was retired on
    2026-08-27 because a US-only universe is always closed at 03:05, so it
    could never do anything -- and hanging the snapshots off the *intraday*
    run instead would have produced ninety operational records a session,
    each describing the same day.
    """
    last = {"ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
            "monitoring-snapshot": pd.Timestamp("2026-08-25 03:05", tz="UTC")}
    due = daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), last)
    assert "monitoring-snapshot" not in due
    assert "risk-snapshot" not in due
    assert "prune-runs" not in due


def test_quotes_are_owed_every_five_minutes():
    last = {"poll-live-quotes": pd.Timestamp("2026-08-25 04:00", tz="UTC"),
            "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
            "monitoring-snapshot": pd.Timestamp("2026-08-25 03:05", tz="UTC")}
    at = lambda m: pd.Timestamp("2026-08-25 04:00", tz="UTC") + pd.Timedelta(minutes=m)

    assert daemon.jobs_due(at(4), last) == []
    # The advisor and the paper book ride the same tick: a fresh price is
    # only useful if something notices what it means, and a forward record
    # is only worth having if the decision was written down at a moment it
    # could actually have been acted on.
    assert daemon.jobs_due(at(5), last) == [
        "poll-live-quotes", "advisor-alert", "paper-book",
    ]


# --------------------------------------------------------------------------- #
# Which Trading 212 instrument to look for
# --------------------------------------------------------------------------- #
def test_an_instrument_with_no_cfd_says_so_rather_than_guessing():
    """The mistake this table exists to prevent.

    Nudging a Treasury ETF onto the nearest index is how you end up trading
    something you did not analyse.
    """
    for ticker in ("TLT", "IEF", "HYG", "LQD", "XLK", "XLF", "XLE", "XLV",
                   "EFA", "EEM", "DBC", "^VIX"):
        mapping = t212_instruments.mapping_for(ticker)
        assert mapping.relationship == "none", ticker
        assert not mapping.name, ticker
        assert not mapping.tradable, ticker


def test_a_suggestion_never_reads_as_a_confirmed_fact():
    """Nothing here was checked against an account, and it must say so."""
    for ticker in ("SPY", "QQQ", "GLD", "EURUSD=X"):
        mapping = t212_instruments.mapping_for(ticker)
        assert mapping.name
        assert not mapping.confirmed, ticker
        assert "unconfirmed" in t212_instruments.describe_mapping(mapping)


def test_your_own_table_wins_and_is_trusted():
    mapping = t212_instruments.mapping_for("SPY", {"SPY": "US 500"})

    assert mapping.name == "US 500"
    assert mapping.confirmed
    assert "unconfirmed" not in t212_instruments.describe_mapping(mapping)


def test_a_tracking_instrument_is_not_described_as_the_same_one():
    """SPY and US 500 follow one market and are two instruments."""
    tracks = t212_instruments.mapping_for("SPY")
    same = t212_instruments.mapping_for("EURUSD=X")

    assert tracks.relationship == "tracks" and not tracks.exact
    assert "not the same instrument" in t212_instruments.describe_mapping(tracks)
    assert same.exact


def test_an_unknown_ticker_is_unmapped_not_absent():
    mapping = t212_instruments.mapping_for("FTSE")

    assert mapping.relationship == "unmapped"
    assert not mapping.tradable
    assert t212_instruments.unmapped(["FTSE", "SPY"]) == ["FTSE"]


# --------------------------------------------------------------------------- #
# The universe, and what may be traded in it
# --------------------------------------------------------------------------- #
def test_every_configured_instrument_is_classified():
    """An unclassified ticker would get no leverage ceiling in the template.

    The universe no longer spans asset classes, and that is the objective
    rather than an omission: every source of non-price evidence this system
    has -- filings, earnings dates, company news, sentiment -- exists for
    companies and cannot exist for an index or a currency pair. Measured
    over two years, the three US indices lost money on their own and their
    removal took the return from 81.3% a year to 126.7%.

    A floor still applies, because a universe that quietly shrank to three
    names would pass every other test here.
    """
    from cfdinvest.config import load_config

    universe = list(load_config().market.tickers)
    assert len(universe) >= 10, "the universe has collapsed"

    classes = {t212_instruments.asset_class(t) for t in universe}
    assert classes == {"share"}, (
        f"the universe is meant to be individual stocks only, found {classes}"
    )

    unknown = [t for t in universe if t212_instruments.asset_class(t) == "unknown"]
    assert not unknown, f"unclassified: {unknown}"
    assert not t212_instruments.unmapped(universe)


def test_every_instrument_in_the_universe_can_actually_be_traded():
    """M148: an untradable name in a universe that is now all tradable.

    ^VIX used to sit here on purpose, as a regime input rather than a
    trade. It left when the engine moved to hourly bars: Yahoo serves only
    about sixty days of hourly VIX against two years for everything else,
    so it would have been a column of holes in a panel whose density is the
    whole reason the universe was narrowed.

    Nothing was lost with it. The volatility regime is computed from the
    instruments' own realised volatility and ATR -- see
    ``regimes.volatility.classify_volatility``, which takes price panels and
    never reads the VIX -- and FRED's VIXCLS remains available to the macro
    classifier. Measured after the change: 26,268 signal reasons still name
    a volatility regime.

    So the universe is now entirely tradable, and this asserts that rather
    than carrying an exception nobody needs.
    """
    from cfdinvest.config import load_config

    universe = list(load_config().market.tickers)
    untradable = [
        t for t in universe if not t212_instruments.mapping_for(t).tradable
    ]
    assert untradable == [], f"cannot be traded on Trading 212: {untradable}"


def test_an_untradable_instrument_is_never_given_a_position():
    """The backtest must not score P&L on a position that could not exist.

    This is the expensive half of the tradability gate. A recommendation you
    cannot act on merely wastes your time; a *backtest* that trades the VIX
    reports a strategy nobody could have run.
    """
    import numpy as np
    from cfdinvest.signals.frame import SignalConfig, generate_signals
    from cfdinvest.preprocessing.frame import AnalysisFrame

    days = pd.date_range("2024-01-01", periods=260, freq="B", tz="UTC")
    columns = ["^GSPC", "^VIX"]
    # A hard, clean uptrend in both, so the trend engine wants them both.
    rising = pd.DataFrame(
        np.linspace(100, 300, len(days))[:, None].repeat(2, axis=1),
        index=days, columns=columns,
    )
    frame = AnalysisFrame(
        prices={"close": rising, "high": rising * 1.01, "low": rising * 0.99},
        returns={}, volatility={}, correlations={}, macro={}, metadata={},
    )

    signals = generate_signals(frame, config=SignalConfig())

    assert (signals.direction["^VIX"] == "flat").all(), "the VIX was traded"
    assert (signals.size["^VIX"] == 0).all()
    # And the gate is targeted, not a blanket off-switch.
    assert (signals.direction["^GSPC"] != "flat").any(), "the gate caught everything"


def test_the_gate_can_be_lifted_for_research_only():
    """Off is available, and is not the default."""
    from cfdinvest.signals.frame import SignalConfig

    assert SignalConfig().require_tradable is True


def test_the_shipped_rules_cover_every_tradable_instrument():
    """One table, in the repository, covering the configured universe.

    There were three copies of this: a commented-out template, a gitignored
    working copy, and the live file on the server, kept in step by somebody
    remembering to copy one to the other. The deploy installs it now, so the
    repository's copy is the only one that can be edited.
    """
    import tomllib

    from cfdinvest.config import load_config

    document = tomllib.loads(
        Path("config/t212_rules.toml").read_text(encoding="utf-8")
    )
    for ticker in load_config().market.tickers:
        if t212_instruments.mapping_for(ticker).tradable:
            assert ticker in document, f"{ticker} has no recorded rules"


def test_the_deploy_installs_the_rules_table():
    """Otherwise the live copy drifts, which is how it worked before."""
    body = Path("deployment/deploy.sh").read_text(encoding="utf-8")
    assert "config/t212_rules.toml" in body
    assert ".config/cfdinvest/t212_rules.toml" in body


def test_the_daemon_can_actually_invoke_every_job_it_schedules():
    """The bug two deployments found, and the suite did not.

    The daemon handed each job its own argument namespace. Every subcommand
    defines flags the daemon parser does not -- run-once has --force,
    monitor has --no-store, risk-snapshot has --balance -- so all three
    raised AttributeError on their first run.

    The loop caught each one, as it is meant to, so the service stayed
    active, logged a failure a day and did no work. Error containment
    turning a hard failure into a silent one is the thing to watch for.
    """
    from cfdinvest.schedule.app import _job_args, build_parser

    daemon_args = build_parser().parse_args(["--no-email", "daemon"])

    expected = {
        "run-once": ("force", "refresh", "trade_alerts", "no_save"),
        "monitor": ("no_store", "alert", "watch", "interval_minutes"),
        "risk-snapshot": ("balance", "no_store", "watch", "max_observations"),
        "poll-live-quotes": ("account",),
    }
    for command, flags in expected.items():
        namespace = _job_args(daemon_args, command)
        for flag in flags:
            assert hasattr(namespace, flag), f"{command} reads --{flag}"
        # The globals the daemon was started with survive into every job.
        assert namespace.no_email is True, command


def test_the_daemons_job_names_all_map_to_something_runnable():
    """A job in the schedule with no branch would be a silent no-op."""
    import inspect

    from cfdinvest.schedule import daemon as daemon_module
    from cfdinvest.schedule import app as app_module

    # The dispatch lives in run_daemon_job, lifted out of the loop so it
    # can be called by a test at all -- a branch nothing can call is a
    # branch nothing can check.
    body = inspect.getsource(app_module.run_daemon_job)
    for job in daemon_module.JOBS:
        assert f'"{job}"' in body, f"the daemon schedules {job} but cannot run it"


# --------------------------------------------------------------------------- #
# Persisting the analysis frame
# --------------------------------------------------------------------------- #
def test_a_correlation_panel_is_labelled_without_a_string_per_row():
    """The bug that OOM-killed a deployment four times over.

    A correlation panel is (timestamp, ticker) x ticker, so it stacks to
    bars x n x n rows -- quadratic in the universe, which is why it survived
    25 instruments and died at 43. The series column was built with a
    row-wise `agg("|".join, axis=1)`, allocating a Python string per row: 4.17
    million of them, three gigabytes and climbing, to write a file of 512K.

    The labels come from the index's own levels, so there are only n x n of
    them however many bars there are. Storing codes into that small set is
    the fix, and this asserts the *property* -- distinct labels bounded by
    the levels, not the rows -- rather than the dtype.
    """
    import numpy as np
    from cfdinvest.persist.writers import _to_long

    bars, n = 40, 12
    index = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-01", periods=bars, freq="B", tz="UTC"),
         [f"T{i}" for i in range(n)]],
        names=["timestamp", "ticker"],
    )
    panel = pd.DataFrame(
        np.random.rand(len(index), n), index=index,
        columns=[f"T{i}" for i in range(n)],
    )

    long = _to_long({"correlations.matrix": panel}, "value")

    assert len(long) == bars * n * n
    labels = long["series"]
    assert labels.nunique() == n * n
    assert set(labels.iloc[:3]) <= {f"T{a}|T{b}" for a in range(n) for b in range(n)}

    # The labels must not cost one object per row. A categorical holds codes
    # into the small set; anything row-shaped here is the old bug returning.
    stored = getattr(labels, "dtype", None)
    assert str(stored) == "category", (
        "the series column should be dictionary-encoded, not one string per row"
    )
    assert len(labels.cat.categories) == n * n


def test_a_price_panel_still_labels_by_ticker():
    """The two-level case must keep working, unjoined."""
    import numpy as np
    from cfdinvest.persist.writers import _to_long

    days = pd.date_range("2024-01-01", periods=10, freq="B", tz="UTC")
    panel = pd.DataFrame(
        np.random.rand(len(days), 3), index=days, columns=["SPY", "GLD", "TLT"]
    )

    long = _to_long({"prices.close": panel}, "value")

    assert set(long["series"].astype(str)) == {"SPY", "GLD", "TLT"}
    assert "|" not in "".join(long["series"].astype(str).unique())
    assert long["panel"].unique().tolist() == ["prices.close"]


def test_an_unreadable_dotenv_is_ignored_not_fatal(tmp_path, monkeypatch):
    """A .env belonging to someone else must not stop the process starting.

    `Path.is_file()` raises PermissionError rather than returning False, so
    a service running as one user, in a directory holding another user's
    .env, crashed on import. The environment and the config file already
    carry everything needed -- an unreadable .env is the same as none.
    """
    import os
    import stat

    from cfdinvest.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text("SECRET=value\n", encoding="utf-8")

    assert load_dotenv(env) == {"SECRET": "value"}

    # A directory stands in for "exists but cannot be read as a file",
    # because chmod is not reliably enforced for the owner or on Windows.
    assert load_dotenv(tmp_path) == {}
    assert load_dotenv(tmp_path / "absent.env") == {}


# --------------------------------------------------------------------------- #
# Keeping the disk from filling
# --------------------------------------------------------------------------- #
def test_the_newest_run_is_never_touched_however_the_policy_is_set():
    """A retention policy that can delete what the dashboard is about to
    open is a bug with a schedule attached."""
    from cfdinvest.persist import retention

    runs = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]

    for keep_full, keep_runs in ((0, 0), (1, 1), (-5, -5)):
        plan = retention.plan_pruning(runs, keep_full=keep_full, keep_runs=keep_runs)
        assert plan.keep_whole[0] == "20260103T000000Z", (keep_full, keep_runs)
        assert "20260103T000000Z" not in plan.slim
        assert "20260103T000000Z" not in plan.delete


def test_recent_runs_keep_everything_and_older_ones_are_slimmed():
    from cfdinvest.persist import retention

    runs = [f"2026010{n}T000000Z" for n in range(1, 8)]
    plan = retention.plan_pruning(runs, keep_full=2, keep_runs=5)

    assert plan.keep_whole == ("20260107T000000Z", "20260106T000000Z")
    assert plan.slim == ("20260105T000000Z", "20260104T000000Z", "20260103T000000Z")
    assert plan.delete == ("20260102T000000Z", "20260101T000000Z")
    # Nothing is in two buckets at once.
    assert not set(plan.slim) & set(plan.delete)
    assert not set(plan.keep_whole) & set(plan.slim)


def test_only_rebuildable_artefacts_are_removed():
    """Both can be produced again -- the analysis frame from the lake,
    attribution from the trades. The record of what the system did is not
    something pruning may take."""
    from cfdinvest.persist import retention

    assert set(retention.HEAVY_ARTEFACTS) == {
        "analysis.parquet", "attribution.parquet"
    }
    for irreplaceable in ("trades.parquet", "equity.parquet", "pnl.parquet",
                          "summary.json", "signals.parquet", "positions.parquet"):
        assert irreplaceable not in retention.HEAVY_ARTEFACTS


def test_pruning_slims_the_heavy_artefacts_and_leaves_the_record(tmp_path):
    from cfdinvest.config import AppConfig, PathConfig
    from cfdinvest.persist import retention

    config = AppConfig(paths=PathConfig(data_root=tmp_path))
    root = tmp_path / "runs"
    for run in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        directory = root / run
        directory.mkdir(parents=True)
        for name in ("analysis.parquet", "attribution.parquet",
                     "trades.parquet", "summary.json"):
            (directory / name).write_bytes(b"x" * 1000)

    result = retention.prune_runs(config, keep_full=1, keep_runs=365)

    newest = root / "20260103T000000Z"
    older = root / "20260101T000000Z"
    assert (newest / "analysis.parquet").exists(), "the newest keeps everything"
    assert not (older / "analysis.parquet").exists()
    assert not (older / "attribution.parquet").exists()
    # The record of the run survives.
    assert (older / "trades.parquet").exists()
    assert (older / "summary.json").exists()
    assert result["freed_bytes"] == 4000


def test_a_dry_run_removes_nothing_but_reports_what_it_would(tmp_path):
    from cfdinvest.config import AppConfig, PathConfig
    from cfdinvest.persist import retention

    config = AppConfig(paths=PathConfig(data_root=tmp_path))
    directory = tmp_path / "runs" / "20260101T000000Z"
    directory.mkdir(parents=True)
    (directory / "analysis.parquet").write_bytes(b"x" * 500)
    (tmp_path / "runs" / "20260102T000000Z").mkdir()

    result = retention.prune_runs(config, keep_full=1, keep_runs=365, dry_run=True)

    assert result["dry_run"] and result["freed_bytes"] == 500
    assert (directory / "analysis.parquet").exists(), "a dry run must remove nothing"


def test_pruning_never_raises_on_an_unreadable_root(tmp_path):
    """It runs after a pipeline run. A failed delete must not lose the run
    that just completed."""
    from cfdinvest.config import AppConfig, PathConfig
    from cfdinvest.persist import retention

    config = AppConfig(paths=PathConfig(data_root=tmp_path / "does-not-exist"))
    result = retention.prune_runs(config)

    assert result["deleted"] == [] and result["slimmed"] == []


def test_the_daemon_prunes_after_the_snapshots_not_before():
    """A run is only pruned once everything describing it has been written."""
    import pandas as pd

    from cfdinvest.schedule import daemon

    due = daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), {})

    assert "prune-runs" in due
    assert due.index("prune-runs") > due.index("monitoring-snapshot")
    assert due.index("prune-runs") > due.index("risk-snapshot")


# --------------------------------------------------------------------------- #
# Alerting: only when it is new
# --------------------------------------------------------------------------- #
def a_suggestion(ticker="AAPL", direction="short", confidence=0.7, tradable=True):
    from cfdinvest.advisor.actions import Suggestion

    return Suggestion(ticker=ticker, direction=direction, confidence=confidence,
                      size_fraction=0.05, price=100.0, cash=500.0, quantity=5.0,
                      t212_name="Apple", tradable=tradable)


def a_watch(ticker="NVDA", severity="warning"):
    from cfdinvest.advisor.actions import Watch

    return Watch(ticker=ticker, quantity=1.0, severity=severity, note="close to stop")


def test_the_same_alert_is_not_sent_twice():
    """An advisor that repeats itself every five minutes is one you stop
    reading -- and then the alert you would have acted on arrives in a thread
    you have learned to ignore."""
    from cfdinvest.advisor import alerts

    first, seen = alerts.decide_alerts([a_suggestion()], [a_watch()], set(),
                                       as_of="2026-08-25")
    assert len(first) == 2

    again, seen_again = alerts.decide_alerts([a_suggestion()], [a_watch()], seen,
                                             as_of="2026-08-25")
    assert again == []
    assert seen_again == seen


def test_a_suggestion_alerts_again_on_a_new_day():
    """Signals come from daily bars, so the same instrument clearing the bar
    tomorrow is a fresh statement."""
    from cfdinvest.advisor import alerts

    _, seen = alerts.decide_alerts([a_suggestion()], [], set(), as_of="2026-08-25")
    tomorrow, _ = alerts.decide_alerts([a_suggestion()], [], seen, as_of="2026-08-26")

    assert len(tomorrow) == 1


def test_a_position_that_recovers_can_alert_again():
    """It drops out of the state when it is no longer in trouble, so sliding
    back towards the stop is a real second event."""
    from cfdinvest.advisor import alerts

    _, seen = alerts.decide_alerts([], [a_watch()], set())
    assert "position|NVDA|warning" in seen

    # Recovered: not in trouble, so not in the new state.
    _, recovered = alerts.decide_alerts([], [a_watch(severity="pass")], seen)
    assert "position|NVDA|warning" not in recovered

    again, _ = alerts.decide_alerts([], [a_watch()], recovered)
    assert len(again) == 1


def test_worsening_from_warning_to_critical_is_a_new_alert():
    from cfdinvest.advisor import alerts

    _, seen = alerts.decide_alerts([], [a_watch(severity="warning")], set())
    worse, _ = alerts.decide_alerts([], [a_watch(severity="critical")], seen)

    assert len(worse) == 1
    assert worse[0].severity == "critical"


def test_an_untradable_suggestion_never_wakes_anyone():
    """Waking someone for an instrument they cannot open is how a channel
    gets muted."""
    from cfdinvest.advisor import alerts

    fresh, _ = alerts.decide_alerts([a_suggestion(tradable=False)], [], set())
    assert fresh == []


def test_the_urgent_thing_is_first():
    from cfdinvest.advisor import alerts

    fresh, _ = alerts.decide_alerts(
        [a_suggestion()],
        [a_watch("A", "warning"), a_watch("B", "critical")],
        set(),
    )
    assert [alert.severity for alert in fresh] == ["critical", "warning", "info"]


def test_unreadable_alert_state_repeats_rather_than_swallows(tmp_path):
    """Of the two failure modes, a duplicate beats a missed alert."""
    from cfdinvest.advisor import alerts

    config = config_for(tmp_path)
    path = alerts._state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert alerts.load_seen(config) == set()


def test_the_alert_email_says_what_to_do_and_what_it_cannot_do():
    from cfdinvest.advisor import alerts
    from cfdinvest.schedule import notify

    fresh, _ = alerts.decide_alerts([a_suggestion()], [a_watch()], set())
    body = notify.build_advisor_alert(fresh).get_content()

    assert "Apple" in body and "units" in body
    assert "Live-Execution Helper" in body
    # The constraint that has held since the first commit.
    assert "places no trades" in body


def test_the_advisor_runs_on_the_quote_cadence():
    """A fresh price is only useful if something notices what it means."""
    import pandas as pd

    from cfdinvest.schedule import daemon

    due = daemon.jobs_due(pd.Timestamp("2026-08-25 12:00", tz="UTC"), {})
    assert "advisor-alert" in due
    assert due.index("advisor-alert") > due.index("poll-live-quotes")


def test_the_scheduler_never_imports_the_dashboard():
    """The advisor logic sits outside ui/ on purpose: the dashboard is an
    optional install and the notifier has to work where it was never put."""
    for path in (Path("cfdinvest/advisor/actions.py"),
                 Path("cfdinvest/advisor/alerts.py"),
                 Path("cfdinvest/schedule/app.py"),
                 Path("cfdinvest/schedule/notify.py")):
        imported = _imports(path)
        assert not [m for m in imported if m.startswith("cfdinvest.ui")], path
        assert not [m for m in imported if m == "streamlit"], path


def test_every_configured_instrument_has_real_trading_hours():
    """The hard-coded list I said did not exist.

    DEFAULT_ASSET_CLASSES still described the old universe after it changed,
    so 34 of 41 instruments fell through to "other" and were gated on the
    wrong session -- an index in Frankfurt treated like a US share.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.market_calendar import asset_class_of

    universe = list(load_config().market.tickers)
    unclassified = [t for t in universe if asset_class_of(t) == "other"]

    assert not unclassified, f"no trading hours for {unclassified}"


def test_no_asset_class_points_at_a_session_that_does_not_exist():
    """A class with no rule used to raise KeyError, which stops the whole
    scheduler rather than the one instrument it could not classify."""
    from cfdinvest.schedule.market_calendar import (
        DEFAULT_ASSET_CLASSES, SESSIONS, session_for,
    )

    for ticker, kind in DEFAULT_ASSET_CLASSES.items():
        assert kind in SESSIONS, f"{ticker} maps to {kind}, which has no session"

    # And an unknown one falls back rather than raising.
    assert session_for("NOT-A-REAL-TICKER") is SESSIONS["other"]


def test_the_advisor_sizes_its_alerts_from_the_configured_balance():
    """It said "About size unknown", which is the one thing it exists to
    tell you: the daemon passed no balance, so cash and quantity were None."""
    import inspect

    from cfdinvest.schedule import app

    body = inspect.getsource(app._command_advisor_alert)

    assert "load_live_helper_config" in body
    assert "account_balance_gbp" in body


# --------------------------------------------------------------------------- #
# The Trading 212 ticket, worked out beforehand
# --------------------------------------------------------------------------- #
def test_margin_is_the_gbp_notional_divided_by_leverage():
    from cfdinvest.livehelper.ticket import build_ticket

    ticket = build_ticket(
        "NKE", "short", quantity=10.0, price=100.0,
        currency="USD", fx_rate=0.80, leverage=5.0, financing_rate=0.0,
    )

    assert ticket.notional == pytest.approx(1000.0)      # 10 x 100 USD
    assert ticket.notional_gbp == pytest.approx(800.0)   # x 0.80
    assert ticket.margin_gbp == pytest.approx(160.0)     # / 5
    assert ticket.complete


def test_a_missing_broker_figure_is_named_never_assumed():
    """A margin computed from an assumed leverage looks exactly as
    authoritative as a real one, and is wrong by a multiple."""
    from cfdinvest.livehelper.ticket import build_ticket

    ticket = build_ticket("NKE", "short", 10.0, 100.0, currency="USD")

    assert ticket.margin_gbp is None
    assert ticket.notional_gbp is None
    assert ticket.financing_per_day_gbp is None
    assert set(ticket.missing) == {"fx_rate", "leverage", "financing_rate"}
    assert not ticket.complete
    # The instrument-currency notional needs nothing from the broker.
    assert ticket.notional == pytest.approx(1000.0)


def test_a_gbp_instrument_needs_no_rate_and_pays_no_fx_fee():
    """It converts nothing. Charging the fee anyway would invent a cost the
    ticket will not show."""
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    ticket = build_ticket(
        "^FTSE", "long", 1.0, 9100.0, currency="GBP",
        leverage=20.0, financing_rate=0.0, fx_fee_applies=False,
    )
    assert "fx_rate" not in ticket.missing
    assert ticket.notional_gbp == pytest.approx(9100.0)

    result = close_out(ticket, close_price=9200.0, days_held=0)
    assert result.fx_fee_gbp == 0.0
    assert result.net_pnl_gbp == pytest.approx(100.0)


def test_the_fx_fee_is_charged_on_the_result_and_on_a_loss_too():
    """Trading 212 applies 0.5% to the profit or loss at close. It is a
    conversion cost, not a tax on winning, so a loss pays it as well."""
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    ticket = build_ticket(
        "NKE", "long", 10.0, 100.0, currency="USD",
        fx_rate=1.0, leverage=5.0, financing_rate=0.0, fx_fee_applies=True,
    )

    won = close_out(ticket, close_price=110.0)
    lost = close_out(ticket, close_price=90.0)

    assert won.gross_pnl_gbp == pytest.approx(100.0)
    assert won.fx_fee_gbp == pytest.approx(0.5)      # 0.5% of the result
    assert won.net_pnl_gbp == pytest.approx(99.5)

    assert lost.gross_pnl_gbp == pytest.approx(-100.0)
    assert lost.fx_fee_gbp == pytest.approx(0.5)     # charged, not credited
    assert lost.net_pnl_gbp == pytest.approx(-100.5)


def test_the_fee_percentage_comes_from_the_fx_layer_not_a_second_copy():
    """One owner per number. Two copies drift, and the day they drift the
    ticket says one thing and the backtest another."""
    from cfdinvest.execution.fx import FXConfig
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    ticket = build_ticket("X", "long", 1.0, 100.0, currency="USD",
                          fx_rate=1.0, leverage=1.0, financing_rate=0.0,
                          fx_fee_applies=True)
    result = close_out(ticket, close_price=200.0)

    assert result.fx_fee_gbp == pytest.approx(100.0 * FXConfig().fx_conversion_fee_pct)


def test_a_short_makes_money_when_the_price_falls():
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    ticket = build_ticket("NKE", "short", 10.0, 100.0, currency="GBP",
                          leverage=5.0, financing_rate=0.0, fx_fee_applies=False)

    assert close_out(ticket, 90.0).gross_pnl_gbp == pytest.approx(100.0)
    assert close_out(ticket, 110.0).gross_pnl_gbp == pytest.approx(-100.0)


def test_financing_accrues_on_calendar_days():
    """A position held over a weekend pays for the weekend."""
    from cfdinvest.livehelper.ticket import DAYS_PER_YEAR, build_ticket, close_out

    ticket = build_ticket("X", "long", 1.0, 36500.0, currency="GBP",
                          leverage=1.0, financing_rate=0.10, fx_fee_applies=False)

    assert DAYS_PER_YEAR == 365.0
    assert ticket.financing_per_day_gbp == pytest.approx(10.0)
    assert close_out(ticket, 36500.0, days_held=7).financing_gbp == pytest.approx(70.0)


# --------------------------------------------------------------------------- #
# Correcting a recorded trade
# --------------------------------------------------------------------------- #
def test_a_correction_replaces_the_position_and_keeps_the_history():
    """Confirmations are append-only, so nothing edits history.

    The replay is last-write-wins per instrument, so recording the same
    instrument again with the right numbers fixes the book while leaving
    what you originally typed on the record. That trail is the point: it is
    how you later find out you keep mistyping the size.
    """
    from cfdinvest.persist.confirm import Confirmation, confirmed_positions

    wrong = Confirmation.of(
        "NKE", "open", direction="short", quantity=2.0, price=68.0,
        currency="USD", fx_rate=0.79, leverage=5.0,
        timestamp=pd.Timestamp("2026-08-25 10:00", tz="UTC"),
    )
    right = Confirmation.of(
        "NKE", "open", direction="short", quantity=2.6872, price=68.75,
        currency="USD", fx_rate=0.79, leverage=5.0,
        timestamp=pd.Timestamp("2026-08-25 11:00", tz="UTC"),
    )

    book = confirmed_positions([wrong, right])

    assert book["NKE"]["quantity"] == pytest.approx(-2.6872)
    assert book["NKE"]["price"] == pytest.approx(68.75)
    # Order of arrival must not matter; time order decides.
    assert confirmed_positions([right, wrong])["NKE"]["quantity"] == pytest.approx(
        -2.6872
    )


def test_the_book_carries_the_terms_a_position_was_opened_on():
    """So a correction pre-fills with them instead of being retyped, and the
    GBP figures can be recomputed without guessing."""
    from cfdinvest.persist.confirm import Confirmation, confirmed_positions

    book = confirmed_positions([
        Confirmation.of("NKE", "open", direction="short", quantity=1.0,
                        price=68.75, currency="USD", fx_rate=0.79,
                        leverage=5.0, financing_rate=0.0295)
    ])

    entry = book["NKE"]
    assert entry["currency"] == "USD"
    assert entry["fx_rate"] == pytest.approx(0.79)
    assert entry["leverage"] == pytest.approx(5.0)
    assert entry["financing_rate"] == pytest.approx(0.0295)


def test_a_confirmation_written_before_the_terms_existed_still_loads():
    """Additive with None defaults. None means not recorded -- never a
    default that lets a GBP figure be computed from a number nobody gave."""
    from cfdinvest.persist.confirm import Confirmation

    old = Confirmation.from_document({
        "ticker": "NKE", "action": "open", "quantity": -1.0, "price": 68.0,
        "timestamp": "2026-08-01T00:00:00+00:00",
    })

    assert old.currency is None and old.fx_rate is None
    assert old.leverage is None and old.financing_rate is None


# --------------------------------------------------------------------------- #
# What your recorded trades actually returned
# --------------------------------------------------------------------------- #
def a_confirmation(ticker, action, **kwargs):
    from cfdinvest.persist.confirm import Confirmation

    return Confirmation.of(ticker, action, **kwargs)


def test_every_configured_instrument_can_actually_be_recorded():
    """22 of 41 could not be.

    Yahoo carries ^ for an index and = for a future or FX pair, and the
    ticker pattern allowed neither -- so the form refused the trade you had
    just placed, for more than half the universe.
    """
    from cfdinvest.config import load_config
    from cfdinvest.persist.confirm import _TICKER

    rejected = [t for t in load_config().market.tickers if not _TICKER.match(t)]
    assert not rejected, rejected


def test_a_ticker_still_cannot_be_a_path_or_a_shell_fragment():
    from cfdinvest.persist.confirm import _TICKER

    for nasty in ("../etc/passwd", "a/b", "x;rm -rf /", "", " ", "a" * 40):
        assert not _TICKER.match(nasty), nasty


def test_a_correction_does_not_become_a_second_round_trip():
    """A second open on an instrument already open restates the same
    position. Counting it as a new trade would invent one that never
    happened -- and would do it every time you fix a typo."""
    from cfdinvest.confirm.performance import realised_trades

    trips = realised_trades([
        a_confirmation("NKE", "open", direction="short", quantity=2.0,
                       price=68.0, currency="GBP", financing_rate=0.0, fx_fee_applies=False,
                       timestamp=pd.Timestamp("2026-08-01 10:00", tz="UTC")),
        a_confirmation("NKE", "open", direction="short", quantity=3.0,
                       price=69.0, currency="GBP", financing_rate=0.0, fx_fee_applies=False,
                       timestamp=pd.Timestamp("2026-08-01 11:00", tz="UTC")),
        a_confirmation("NKE", "close", price=60.0,
                       timestamp=pd.Timestamp("2026-08-02 11:00", tz="UTC")),
    ])

    assert len(trips) == 1
    # Valued on the corrected entry, not the mistyped one.
    assert trips[0].quantity == pytest.approx(3.0)
    assert trips[0].open_price == pytest.approx(69.0)


def test_an_open_position_is_not_counted_as_a_result():
    """An unrealised move is not a result, and counting it as one is how a
    losing book reports a win rate."""
    from cfdinvest.confirm.performance import realised_trades

    trips = realised_trades([
        a_confirmation("NKE", "open", direction="long", quantity=1.0, price=10.0,
                       timestamp=pd.Timestamp("2026-08-01", tz="UTC")),
    ])
    assert trips == []


def test_costs_are_taken_out_of_the_recorded_result():
    """The gap this closes. On the backtest, financing was the largest cost
    line -- a figure computed from price movement alone flatters a
    leveraged, multi-week strategy by exactly what it costs to carry."""
    from cfdinvest.confirm.performance import realised_trades, summarise

    trips = realised_trades([
        a_confirmation("X", "open", direction="long", quantity=1.0, price=36500.0,
                       currency="GBP", leverage=1.0, financing_rate=0.10,
                       fx_fee_applies=False,
                       timestamp=pd.Timestamp("2026-08-01", tz="UTC")),
        a_confirmation("X", "close", price=36500.0,
                       timestamp=pd.Timestamp("2026-08-11", tz="UTC")),
    ])

    trip = trips[0]
    assert trip.gross_pnl_gbp == pytest.approx(0.0)   # price did not move
    assert trip.financing_gbp == pytest.approx(100.0)  # 10 days at £10
    assert trip.net_pnl_gbp == pytest.approx(-100.0)   # and it cost you

    total = summarise(trips)
    assert total["net_pnl_gbp"] == pytest.approx(-100.0)
    assert total["costs_gbp"] == pytest.approx(100.0)


def test_a_trip_missing_its_terms_is_reported_not_dropped():
    """A win rate over the trades that happened to carry an exchange rate is
    not a win rate, and the difference is invisible unless it is stated."""
    from cfdinvest.confirm.performance import realised_trades, summarise

    trips = realised_trades([
        a_confirmation("NKE", "open", direction="long", quantity=1.0, price=10.0,
                       currency="USD",  # no fx_rate, no financing rate
                       timestamp=pd.Timestamp("2026-08-01", tz="UTC")),
        a_confirmation("NKE", "close", price=12.0,
                       timestamp=pd.Timestamp("2026-08-02", tz="UTC")),
    ])

    assert len(trips) == 1
    assert not trips[0].complete
    assert trips[0].won is None, "unvalued is not a loss"

    total = summarise(trips)
    assert total["valued"] == 0 and total["unvalued"] == 1
    assert total["win_rate"] is None
    assert "fx_rate" in total["missing"]


def test_leverage_alone_missing_does_not_stop_a_trip_being_valued():
    """Leverage decides the margin, not the result."""
    from cfdinvest.confirm.performance import realised_trades

    trips = realised_trades([
        a_confirmation("X", "open", direction="long", quantity=1.0, price=100.0,
                       currency="GBP", financing_rate=0.0, fx_fee_applies=False,
                       timestamp=pd.Timestamp("2026-08-01", tz="UTC")),
        a_confirmation("X", "close", price=110.0,
                       timestamp=pd.Timestamp("2026-08-02", tz="UTC")),
    ])

    assert trips[0].complete
    assert trips[0].net_pnl_gbp == pytest.approx(10.0)


def test_an_expiring_contract_can_be_recorded_as_one():
    """Trading 212's index CFDs expire -- "UK 100 EXPIRING 17 SEPTEMBER
    2026" -- rather than running perpetually.

    It matters twice: a position cannot be held through the date, and a
    dated contract usually carries its funding in the forward price rather
    than charging overnight interest. Recorded so the difference is visible.
    Nothing infers one from the other.
    """
    from cfdinvest.livehelper.t212_rules import InstrumentRules

    dated = InstrumentRules(ticker="^FTSE", leverage=20.0, expiry="2026-09-17")
    perpetual = InstrumentRules(ticker="EURUSD=X", leverage=30.0)

    assert dated.expiry == "2026-09-17"
    assert perpetual.expiry is None
    # An expiry is not a financing rate and must not imply one either way.
    assert dated.financing_rate is None


def test_a_verified_name_overrides_the_shipped_guess():
    """The shipped names are hand-written and at least one was wrong.

    "US 500" against the app's "USA 500" is exactly the error that sends you
    to the wrong instrument or to none at all, so what you read in the app
    has to win.
    """
    from cfdinvest.livehelper.t212_instruments import (
        display_name, from_rules, mapping_for,
    )
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    table = RuleTable(rules={
        "^GSPC": InstrumentRules(ticker="^GSPC", t212_name="USA 500")
    })
    overrides = from_rules(table)

    assert display_name("^GSPC", overrides) == "USA 500 (^GSPC)"
    assert mapping_for("^GSPC", overrides).confirmed


def test_a_rule_without_a_name_does_not_launder_a_guess():
    """An entry recording only a leverage says nothing about what the
    instrument is called; treating it as confirmation would mark a
    hand-written guess as checked."""
    from cfdinvest.livehelper.t212_instruments import from_rules, mapping_for
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    table = RuleTable(rules={
        "^GSPC": InstrumentRules(ticker="^GSPC", leverage=20.0)
    })
    overrides = from_rules(table)

    assert "^GSPC" not in overrides
    assert not mapping_for("^GSPC", overrides).confirmed


def test_an_instrument_recorded_as_unavailable_stops_being_suggested():
    from cfdinvest.livehelper.t212_instruments import from_rules, mapping_for
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    table = RuleTable(rules={
        "LULU": InstrumentRules(ticker="LULU", available=False)
    })
    mapping = mapping_for("LULU", from_rules(table))

    assert not mapping.tradable
    assert mapping.relationship == "none"


def test_the_overnight_fee_is_recorded_as_the_app_shows_it():
    """Trading 212 displays a percentage per day; financing_rate is an
    annual fraction.

    Typing 0.0083 into the annual field is wrong by about four thousand
    times, and it produces a plausible small number rather than an obviously
    broken one -- which is the dangerous kind of wrong. So the daily figure
    is recorded as read and converted once, here.
    """
    from cfdinvest.livehelper.t212_rules import InstrumentRules

    uk100 = InstrumentRules(ticker="^FTSE", financing_daily_pct=0.0083)

    assert uk100.annual_financing_rate == pytest.approx(0.030295)
    # Sanity: a GBP index at roughly 3% a year, not 0.83% and not 300%.
    assert 0.02 < uk100.annual_financing_rate < 0.05


def test_the_daily_figure_wins_when_both_are_given():
    """It is the one read off a screen rather than worked out."""
    from cfdinvest.livehelper.t212_rules import InstrumentRules

    both = InstrumentRules(
        ticker="X", financing_daily_pct=0.0083, financing_rate=0.99
    )
    assert both.annual_financing_rate == pytest.approx(0.030295)


def test_an_unrecorded_financing_rate_stays_unknown_not_zero():
    """Zero would say the carry is free, which is a measurement nobody made
    -- and carry is the largest cost line in the backtest."""
    from cfdinvest.livehelper.t212_rules import InstrumentRules

    assert InstrumentRules(ticker="X").annual_financing_rate is None


def test_whether_the_fx_fee_applies_is_recorded_not_inferred():
    """Real data got my inference backwards.

    USA 500 is funded in sterling and charges the 0.5% conversion on the
    result, because the market underneath is American. UK 100 is funded in
    sterling and does not. Reading it off the quoted currency would have
    called both the same.
    """
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    charged = build_ticket("^GSPC", "long", 1.0, 100.0, currency="GBP",
                           leverage=20.0, financing_rate=0.0,
                           fx_fee_applies=True)
    free = build_ticket("^FTSE", "long", 1.0, 100.0, currency="GBP",
                        leverage=20.0, financing_rate=0.0,
                        fx_fee_applies=False)

    assert close_out(charged, 110.0).fx_fee_gbp == pytest.approx(0.05)
    assert close_out(free, 110.0).fx_fee_gbp == 0.0


def test_an_unrecorded_fx_fee_is_unknown_not_guessed_from_the_currency():
    """The guess was that a non-GBP instrument converts and therefore pays.

    Real tickets say otherwise in both directions: USA 500 is funded in
    sterling and charges it, while Germany 40 and France 40 are priced in
    euros and do not. Currency predicted nothing, so an unrecorded flag is
    unknown -- the fee is reported absent and the net result withheld,
    rather than a plausible cost being invented.
    """
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    unknown = build_ticket("^GDAXI", "long", 1.0, 100.0, currency="EUR",
                           fx_rate=0.85, leverage=20.0, financing_rate=0.0)
    result = close_out(unknown, 110.0)

    assert result.fx_fee_gbp is None
    assert result.net_pnl_gbp is None
    assert "fx_fee_applies" in result.missing
    # The gross is still knowable and still reported.
    assert result.gross_pnl_gbp == pytest.approx(8.5)


def test_the_crossing_cost_is_signed_by_disadvantage_not_by_direction():
    """Buying above the quote and selling below it both cost you."""
    from cfdinvest.livehelper.ticket import crossing_cost

    bought_high = crossing_cost(101.0, 100.0, 2.0, direction="long")
    sold_low = crossing_cost(99.0, 100.0, 2.0, direction="short")
    bought_well = crossing_cost(99.0, 100.0, 2.0, direction="long")

    assert bought_high == pytest.approx(2.0)
    assert sold_low == pytest.approx(2.0)
    assert bought_well == pytest.approx(-2.0), "a good fill reads negative"


def test_no_reference_means_no_crossing_cost_not_a_free_one():
    from cfdinvest.livehelper.ticket import crossing_cost

    assert crossing_cost(101.0, None, 2.0) is None
    assert crossing_cost(None, 100.0, 2.0) is None


def test_the_crossing_cost_is_never_deducted_from_the_result():
    """The spread is already inside the fill price. A P&L computed from what
    you actually paid has paid it once; subtracting this too counts it
    twice."""
    import inspect

    from cfdinvest.confirm import performance
    from cfdinvest.livehelper import ticket

    # Nothing in the valuation path may reach for it.
    assert "crossing_cost" not in inspect.getsource(performance)
    assert "crossing_cost" not in inspect.getsource(ticket.close_out)


def test_the_reference_is_recorded_so_the_cost_can_be_rechecked():
    from cfdinvest.persist.confirm import Confirmation

    recorded = Confirmation.of(
        "NKE", "open", direction="short", quantity=2.0, price=68.20,
        reference_price=68.44, reference_age_minutes=3.0,
    )

    assert recorded.reference_price == pytest.approx(68.44)
    assert recorded.reference_age_minutes == pytest.approx(3.0)
    # Absent on a record that predates it, rather than defaulted.
    without = Confirmation.of("NKE", "open", quantity=1.0, price=68.20)
    assert without.reference_price is None
    assert without.reference_age_minutes is None


def test_the_fx_fee_defaults_to_the_basis_trading_212_actually_uses():
    """0.5% of the profit or loss at close, not of the position value.

    Confirmed against the app on every instrument recorded. The difference
    is fifty-fold on a typical trade -- £25 against £0.50 on a £5,000
    position closed at £100 profit -- so defaulting to the wrong one would
    make every backtest of a foreign-currency instrument unreadable.
    """
    from cfdinvest.execution.fx import FX_FEE_BASES, FXConfig

    assert FXConfig().fx_fee_basis == "result"
    assert set(FX_FEE_BASES) == {"notional", "result"}, "both stay available"
    assert FXConfig().fx_conversion_fee_pct == pytest.approx(0.005)


def test_the_paper_books_open_positions_are_worth_a_quote(tmp_path, monkeypatch):
    """M160: the forward record showing as unpriceable on its own page.

    ``instruments_to_price`` covered the confirmed book and anything
    clearing the confidence bar, but not the paper book -- so with no real
    position open the poller wrote a quote file containing nothing the
    dashboard needed, and every paper position marked as unpriced.

    The paper pass does poll its own prices, but in memory and discards
    them. Asking here keeps one answer to "what is worth a quote" instead
    of two that can drift apart.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livequotes.poller import instruments_to_price

    config = config_for(tmp_path)
    monkeypatch.setattr(
        papertrade, "_path",
        lambda cfg=None, account=None: tmp_path / "paper" / "book.jsonl",
    )

    opened = papertrade.open_trade(
        "NKE", "short", 60.0, "2026-08-26T16:06:00Z",
        rules=None, fx_rate=0.7355, cohort="c50",
    )
    assert opened is not None
    papertrade.record([opened], config)

    wanted = instruments_to_price(config)
    assert "NKE" in wanted, "an open paper position was not priced"

    # And once it closes it stops being polled -- this must not accumulate
    # every instrument the book has ever touched.
    closed = papertrade.value_trade(opened.as_row(), 59.0, "2026-08-26T18:00:00Z")
    papertrade.record([closed], config)
    assert "NKE" not in instruments_to_price(config)
