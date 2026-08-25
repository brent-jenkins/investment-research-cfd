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
        "run-daily": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
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
        "run-daily": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
    }
    assert daemon.jobs_due(pd.Timestamp("2026-08-25 03:30", tz="UTC"), last) == []


def test_the_daily_work_comes_round_again_the_next_day():
    last = {
        "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
        "run-daily": pd.Timestamp("2026-08-25 03:05", tz="UTC"),
    }
    due = daemon.jobs_due(pd.Timestamp("2026-08-26 03:06", tz="UTC"), last)
    assert "ingest-daily" in due and "run-daily" in due


def test_ingestion_is_ordered_before_the_run_that_reads_it():
    due = daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), {})
    assert due.index("ingest-daily") < due.index("run-daily")
    # And the snapshots describe the run, so they come after it.
    assert due.index("run-daily") < due.index("monitoring-snapshot")
    assert due.index("run-daily") < due.index("risk-snapshot")


def test_the_snapshots_are_owed_only_when_the_pipeline_ran():
    """They describe a run. On their own they describe nothing."""
    last = {"ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
            "run-daily": pd.Timestamp("2026-08-25 03:05", tz="UTC")}
    due = daemon.jobs_due(pd.Timestamp("2026-08-25 04:00", tz="UTC"), last)
    assert "monitoring-snapshot" not in due
    assert "risk-snapshot" not in due


def test_quotes_are_owed_every_five_minutes():
    last = {"poll-live-quotes": pd.Timestamp("2026-08-25 04:00", tz="UTC"),
            "ingest-daily": pd.Timestamp("2026-08-25 03:00", tz="UTC"),
            "run-daily": pd.Timestamp("2026-08-25 03:05", tz="UTC")}
    at = lambda m: pd.Timestamp("2026-08-25 04:00", tz="UTC") + pd.Timedelta(minutes=m)

    assert daemon.jobs_due(at(4), last) == []
    assert daemon.jobs_due(at(5), last) == ["poll-live-quotes"]


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
    """An unclassified ticker would get no leverage ceiling in the template."""
    from cfdinvest.config import load_config

    universe = list(load_config().market.tickers)
    assert len(universe) >= 20, "the universe should span the asset classes"

    unknown = [t for t in universe if t212_instruments.asset_class(t) == "unknown"]
    assert not unknown, f"unclassified: {unknown}"
    assert not t212_instruments.unmapped(universe)


def test_the_only_untradable_instrument_is_the_one_we_keep_on_purpose():
    """The VIX earns its place as a regime input, not as a trade."""
    from cfdinvest.config import load_config

    universe = list(load_config().market.tickers)
    untradable = [
        t for t in universe if not t212_instruments.mapping_for(t).tradable
    ]
    assert untradable == ["^VIX"]


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


def test_the_rules_template_covers_the_universe_and_asserts_no_numbers():
    """The template makes filling the table quick without filling it in."""
    from cfdinvest.config import load_config

    body = Path("deployment/scripts/t212_rules.example.toml").read_text(
        encoding="utf-8"
    )
    for ticker in load_config().market.tickers:
        if t212_instruments.mapping_for(ticker).tradable:
            assert f'["{ticker}"]' in body, ticker

    # Every line is a comment: nothing here is a value the helper would read.
    for line in body.splitlines():
        assert not line.strip() or line.lstrip().startswith("#"), line

    assert "regulatory retail ceiling" in body.lower()


def test_the_daemon_can_actually_invoke_every_job_it_schedules():
    """The bug a real deployment found, and the suite did not.

    The daemon handed its own argument namespace to `run-once`, which
    defines flags -- --force, --refresh, --no-save -- that the daemon parser
    does not. Every daily run raised AttributeError. The loop caught it, as
    it is meant to, so the daemon stayed up, logged the failure and never ran
    the pipeline again: contained, and invisible unless you read the journal.
    """
    from cfdinvest.schedule.app import _run_once_args, build_parser

    daemon_args = build_parser().parse_args(["--no-email", "daemon"])
    once = _run_once_args(daemon_args, refresh=False)

    # Every flag run-once reads must be present.
    for flag in ("force", "refresh", "trade_alerts", "no_save"):
        assert hasattr(once, flag), f"run-once reads --{flag}"

    assert once.refresh is False
    # The globals the daemon was invoked with survive into the job.
    assert once.no_email is True


def test_the_daemons_job_names_all_map_to_something_runnable():
    """A job in the schedule with no branch would be a silent no-op."""
    import inspect

    from cfdinvest.schedule import daemon as daemon_module
    from cfdinvest.schedule import app as app_module

    body = inspect.getsource(app_module._command_daemon)
    for job in daemon_module.JOBS:
        assert f'"{job}"' in body, f"the daemon schedules {job} but cannot run it"
