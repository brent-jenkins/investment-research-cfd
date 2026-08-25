"""The read-only dashboard: sources, health checks, formatting and pages.

Two things are worth testing here and they are different in kind.

The **logic** — what counts as stale, what a missing file means, how a
config override is spotted — is plain Python and is tested directly.

The **pages** are Streamlit scripts, which normally means they are only
tested by looking at them. They are executed here against a stub Streamlit
and a synthetic lake, which catches the failure that actually happens: a
page that raises on a column the pipeline did not write.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.ui import components, configview, explain, health, sources, theme

UI = Path(__file__).resolve().parents[1] / "cfdinvest" / "ui"


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
def test_the_dark_palette_is_the_specified_one():
    dark = theme.palette_for("dark")
    assert dark["background"] == "#0B0C10"
    assert dark["panel"] == "#1F2833"
    assert dark["text"] == "#C5C6C7"
    assert dark["accent"] == "#66FCF1"
    assert dark["accent2"] == "#45A29E"


def test_both_modes_exist_and_carry_the_same_roles():
    assert set(theme.PALETTES) == {"light", "dark"}
    assert set(theme.PALETTES["light"]) == set(theme.PALETTES["dark"])


def test_light_mode_inverts_background_and_text():
    """Not the same five colours on white — the roles genuinely swap."""
    light, dark = theme.palette_for("light"), theme.palette_for("dark")
    assert light["background"] != dark["background"]
    assert light["text"] != dark["text"]


def test_the_light_accent_is_readable_on_white():
    """#66FCF1 on white is about 1.3:1. It cannot be the light accent."""

    def luminance(hex_colour: str) -> float:
        r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
        channels = [
            c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            for c in (r, g, b)
        ]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    accent = theme.palette_for("light")["accent"]
    contrast = (1.0 + 0.05) / (luminance(accent) + 0.05)
    assert contrast >= 3.0, f"{accent} is only {contrast:.1f}:1 on white"


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_stylesheet_carries_every_palette_colour(mode):
    css = theme.stylesheet(mode)
    for role, colour in theme.palette_for(mode).items():
        assert colour in css, role


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_stylesheet_is_a_closed_style_block(mode):
    css = theme.stylesheet(mode)
    assert css.strip().startswith("<style>")
    assert css.strip().endswith("</style>")
    assert css.count("{") == css.count("}")


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_stylesheet_is_deterministic(mode):
    assert theme.stylesheet(mode) == theme.stylesheet(mode)


def test_the_two_stylesheets_differ():
    assert theme.stylesheet("light") != theme.stylesheet("dark")


@pytest.mark.parametrize("mode", theme.MODES)
def test_warning_colours_are_not_brand_colours(mode):
    """A warning that shares a hue with the accent is one nobody sees."""
    brand = set(theme.palette_for(mode).values())
    assert theme.colour_for("critical", mode) not in brand
    assert theme.colour_for("warning", mode) not in brand


def test_an_unknown_state_renders_muted_rather_than_raising():
    assert theme.colour_for("something new", "dark") == (
        theme.SEMANTIC_BY_MODE["dark"]["muted"]
    )


def test_an_unknown_mode_falls_back_to_light():
    assert theme.palette_for("chartreuse") == theme.PALETTES["light"]


def test_the_mode_falls_back_when_streamlit_cannot_say():
    """Outside a browser both probes are None; guessing dark blinds people."""
    assert theme.active_mode(None) == "light"


def test_the_mode_reads_the_runtime_first():
    class Probe:
        context = type("C", (), {"theme": type("T", (), {"type": "dark"})()})()

        @staticmethod
        def get_option(name):
            return "light"

    assert theme.active_mode(Probe) == "dark"


def test_a_broken_theme_probe_does_not_stop_the_page():
    class Broken:
        @property
        def context(self):
            raise RuntimeError("no runtime")

        @staticmethod
        def get_option(name):
            raise RuntimeError("no config")

    assert theme.active_mode(Broken()) in theme.MODES


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def test_missing_values_render_as_a_dash_not_nan():
    for value in (None, np.nan, pd.NaT):
        assert components.number(value) == components.DASH
        assert components.money(value) == components.DASH
        assert components.percent(value) == components.DASH
        assert components.moment(value) == components.DASH


def test_money_keeps_the_sign_in_front():
    assert components.money(-1234.5, "£") == "-£1,234.50"
    assert components.money(1234.5, "£") == "£1,234.50"


def test_percent_renders_a_fraction():
    assert components.percent(0.0512) == "5.12%"


def test_times_render_in_the_operators_zone_not_utc():
    """09:30 UTC in August is 10:30 in London, and the operator is in London."""
    assert components.moment("2026-08-24T09:30:00Z") == "24 Aug 2026, 10:30 BST"


def test_naive_input_is_treated_as_utc():
    """Everything this app reads was written in UTC."""
    assert components.moment("2026-08-24T09:30:00") == "24 Aug 2026, 10:30 BST"


def test_winter_renders_as_gmt():
    """The abbreviation is printed so a reader cannot misjudge the offset."""
    assert components.moment("2026-01-15T09:30:00Z") == "15 Jan 2026, 09:30 GMT"


def test_the_short_form_drops_the_year_and_keeps_the_weekday():
    assert components.short_moment("2026-08-24T09:30:00Z") == "Mon 24 Aug, 10:30 BST"


def test_a_date_alone():
    assert components.day("2026-08-24T09:30:00Z") == "Mon 24 Aug 2026"


def test_an_unknown_display_zone_falls_back_to_utc(monkeypatch):
    monkeypatch.setattr(components, "DISPLAY_TZ", "Mars/Olympus_Mons")
    assert components.display_zone().key == "UTC"


def test_age_spells_out_its_units():
    now = pd.Timestamp("2026-08-24 12:00", tz="UTC")
    assert components.age(now - pd.Timedelta(seconds=30), now) == "30 seconds ago"
    assert components.age(now - pd.Timedelta(minutes=30), now) == "30 minutes ago"
    assert components.age(now - pd.Timedelta(hours=6), now) == "6.0 hours ago"
    assert components.age(now - pd.Timedelta(days=4), now) == "4.0 days ago"


def test_a_future_moment_reads_as_future():
    now = pd.Timestamp("2026-08-24 12:00", tz="UTC")
    assert components.age(now + pd.Timedelta(hours=6), now) == "in 6.0 hours"


def test_a_run_identifier_reads_as_a_date():
    """A run id is a compact UTC timestamp, not something to show raw."""
    assert components.run_moment("20260824T090000Z") == "Mon 24 Aug, 10:00 BST"


def test_an_identifier_that_is_not_a_timestamp_is_left_alone():
    assert components.run_moment("nightly-rerun") == "nightly-rerun"
    assert components.run_moment("") == components.DASH


def test_duration_spells_out_its_units():
    assert components.duration(42) == "42.0 seconds"
    assert components.duration(600) == "10.0 minutes"
    assert components.duration(7200) == "2.0 hours"


def test_a_bad_value_never_raises_in_formatting():
    assert components.number("not a number") == "not a number"
    assert components.moment("not a date") == "not a date"


# --------------------------------------------------------------------------- #
# Sources: the Source contract
# --------------------------------------------------------------------------- #
def test_missing_is_not_the_same_as_broken():
    """An empty lake is early; an unreadable one is a fault."""
    missing = sources.Source.missing("nothing yet")
    broken = sources.Source.broken("permission denied")

    assert not missing.failed and not missing.ok
    assert broken.failed and not broken.ok


def test_a_good_read_is_ok():
    assert sources.Source(value=[1, 2]).ok


def test_utcnow_accepts_an_injected_clock():
    pinned = pd.Timestamp("2026-01-01", tz="UTC")
    assert sources.utcnow(pinned) == pinned


def test_utcnow_localises_a_naive_clock():
    assert sources.utcnow(pd.Timestamp("2026-01-01")).tz is not None


# --------------------------------------------------------------------------- #
# Sources: a synthetic lake
# --------------------------------------------------------------------------- #
NOW = pd.Timestamp("2026-08-24 12:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> Path:
    """A lake with one run, one market series and one macro series."""
    root = tmp_path / "data"
    (root / "metadata").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)

    # These are the keys the ingestors actually write -- see
    # ``IngestionReport`` in schema.py. An earlier version of this fixture
    # invented "rows"/"start"/"end", which no real lake contains, and that
    # fiction hid three separate bugs: the monitoring layer and the analytics
    # CLI both looked for column names nothing produced.
    #
    # ``updated_at`` is deliberately *now*, well after the last bar. A lake
    # rewritten this morning from a feed that returned nothing new is stale,
    # and reading the write time would make it look fresh.
    (root / "metadata" / "market_1d_manifest.json").write_text(
        json.dumps(
            {
                "SPX": {"status": "ok", "rows_total": 300,
                        "first_timestamp": "2025-01-02",
                        "last_timestamp": "2026-08-22",
                        "updated_at": "2026-08-24T12:00:00+00:00",
                        "anomalies": {"zero_volume": 2}},
                "UKX": {"status": "ok", "rows_total": 300,
                        "first_timestamp": "2025-01-02",
                        "last_timestamp": "2026-08-22",
                        "updated_at": "2026-08-24T12:00:00+00:00",
                        "anomalies": {}},
            }
        ),
        encoding="utf-8",
    )
    (root / "metadata" / "macro_manifest.json").write_text(
        json.dumps(
            {
                "CPIAUCSL": {
                    "status": "ok", "rows_total": 60,
                    "first_timestamp": "2020-01-01",
                    "last_timestamp": "2026-07-01",
                    "updated_at": "2026-08-24T12:00:00+00:00",
                    "vintage_mode": "publication_lag",
                }
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def run(lake) -> Path:
    """One completed run, with every artefact the pipeline writes."""
    from cfdinvest import storage

    directory = lake / "runs" / "20260824T090000Z"
    directory.mkdir(parents=True)

    index = pd.date_range("2026-08-20", periods=4, tz="UTC", name="timestamp")
    storage.write_parquet(
        pd.DataFrame({"equity": [100_000.0, 100_500.0, 99_800.0, 101_200.0]},
                     index=index),
        directory / "equity.parquet",
    )
    storage.write_parquet(
        pd.DataFrame(
            {"SPX": [0.0, 10.0, 10.0, 10.0], "UKX": [0.0, 0.0, -5.0, -5.0]},
            index=index,
        ),
        directory / "positions.parquet",
    )
    storage.write_parquet(
        pd.DataFrame(
            {
                "timestamp": [index[1], index[2]],
                "asset": ["SPX", "UKX"],
                "action": ["open_long", "open_short"],
                "units": [10.0, -5.0],
                "traded_units": [10.0, -5.0],
                "price": [100.0, 90.0],
                "fill_price": [100.05, 89.95],
                "spread_cost": [1.0, 0.5],
                "slippage": [0.0, 0.0],
                "financing": [0.0, 0.2],
                "realized_pnl": [0.0, 0.0],
                "notional": [1000.0, 450.0],
                "equity": [100_500.0, 99_800.0],
                "reason": ["signal", "signal"],
                "stop_level": [95.0, 94.0],
                "take_profit_level": [110.0, 82.0],
                "stop_fill_price": [np.nan, np.nan],
                "take_profit_fill_price": [np.nan, np.nan],
                "gap_through": [False, False],
                "slippage_applied": [0.0, 0.0],
                "breach_type": ["", ""],
                "fx_rate": [0.8, 1.0],
                "fx_spread": [0.0, 0.0],
                "fx_conversion_fee": [4.0, 0.0],
                "fx_slippage": [0.0, 0.0],
                "fx_adjusted_fill_price": [80.44, 89.95],
            }
        ),
        directory / "trades.parquet",
    )
    signals = pd.DataFrame(
        {
            "direction": ["long", "short"],
            "size": [0.3, 0.25],
            "confidence": [0.9, 0.7],
            "reason": [
                "trend_long @risk_on",
                "trend_short|high_volatility @crisis",
            ],
        },
        index=pd.MultiIndex.from_arrays(
            [[index[-1], index[-1]], ["SPX", "UKX"]], names=["timestamp", "ticker"]
        ),
    )
    storage.write_parquet(signals, directory / "signals.parquet")

    (directory / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "20260824T090000Z",
                "started_at": "2026-08-24T09:00:00+00:00",
                "finished_at": "2026-08-24T09:00:42+00:00",
                "duration_seconds": 42.0,
                "status": "ok",
                "universe": ["SPX", "UKX"],
                "bars": 300,
                "as_of": "2026-08-22T00:00:00+00:00",
                "metrics": {
                    "final_equity": 101_200.0, "total_pnl": 1_200.0,
                    "total_return": 0.012, "max_drawdown_fraction": -0.02,
                    "total_spread": 1.5, "total_slippage": 0.0,
                    "total_financing": 0.2, "total_fx": 4.0,
                },
                "latest_signals": [],
                "latest_trades": [],
                "output_paths": {},
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    return directory


def config_for(lake: Path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=lake))


def test_lake_metadata_counts_series_and_anomalies(lake):
    read = sources.load_lake_metadata(config_for(lake), now=NOW)

    assert read.ok
    assert read.value["series_count"] == 3       # 2 market + 1 macro
    assert read.value["anomalies_count"] == 2
    assert read.value["vintage_mode"] == "publication_lag"


def test_lake_freshness_is_the_newest_observation_not_a_file_time(lake):
    """A lake rewritten today with old data is stale, and must read stale."""
    read = sources.load_lake_metadata(config_for(lake), now=NOW)
    assert read.value["last_updated"] == pd.Timestamp("2026-08-22", tz="UTC")
    assert read.value["age_days"] == pytest.approx(2.5, abs=0.01)


def test_a_rewritten_lake_full_of_old_data_still_reads_stale(lake):
    """The write time must never stand in for the newest bar.

    This is the bug the fixture used to hide. ``updated_at`` in the manifest
    is more recent than any bar, and folding it into the freshness figure
    made a lake that had been rewritten this morning from a dead feed look
    perfectly fresh -- a check that could not fail.
    """
    manifest_path = lake / "metadata" / "market_1d_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest.values():
        entry["updated_at"] = NOW.isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    read = sources.load_lake_metadata(config_for(lake), now=NOW)

    assert read.value["last_updated"] == pd.Timestamp("2026-08-22", tz="UTC")
    assert read.value["age_days"] == pytest.approx(2.5, abs=0.01)


def test_the_monitoring_layer_reads_the_same_lake_the_dashboard_does(lake):
    """Monitoring must not spell the manifest's column names out for itself.

    It used to look for "end"/"last"/"newest"/"end_date", none of which any
    ingestor writes, so on a real lake it returned no metadata and the
    freshness check silently never ran. The dashboard and the monitoring
    layer are separate code paths on purpose -- monitoring has to work where
    the UI was never installed -- but they must agree on the number.
    """
    from cfdinvest.monitoring.collect import _lake_metadata

    metadata = _lake_metadata(config_for(lake))

    assert metadata is not None, "monitoring could not read a readable lake"
    assert metadata["newest"] == pd.Timestamp("2026-08-22", tz="UTC")

    dashboard = sources.load_lake_metadata(config_for(lake), now=NOW)
    assert metadata["newest"] == dashboard.value["last_updated"]


def test_a_missing_lake_is_reported_not_raised(tmp_path):
    read = sources.load_lake_metadata(config_for(tmp_path / "nope"), now=NOW)
    assert not read.ok
    assert "does not exist" in (read.error or read.note or "")


def test_the_last_run_summary_is_found(lake, run):
    read = sources.load_last_pipeline_summary(config_for(lake))
    assert read.ok
    assert read.value["run_id"] == "20260824T090000Z"
    assert read.value["status"] == "ok"


def test_no_runs_is_missing_not_broken(lake):
    read = sources.load_last_pipeline_summary(config_for(lake))
    assert not read.ok
    assert not read.failed


def test_execution_artefacts_load(lake, run):
    read = sources.load_execution_frame(config_for(lake))
    assert read.ok
    assert len(read.value["trades"]) == 2
    assert not read.value["equity"].empty


def test_signals_load_in_long_form(lake, run):
    read = sources.load_signal_frame(config_for(lake))
    assert read.ok
    assert set(read.value.columns) >= {"direction", "size", "confidence", "reason"}


def test_regimes_report_their_own_absence(lake):
    read = sources.load_regime_frame(config_for(lake))
    assert not read.ok
    assert not read.failed
    assert "not persisted" in read.note


def test_the_analysis_frame_reports_its_own_absence(lake):
    assert "not persisted" in sources.load_analysis_frame(config_for(lake)).note


def test_scheduler_status_comes_from_the_run_directory(lake, run):
    read = sources.load_scheduler_status(config_for(lake), ["SPX"], now=NOW)

    assert read.ok
    assert read.value["last_run"] == pd.Timestamp("2026-08-24T09:00:42Z")
    assert read.value["last_duration"] == 42.0
    assert read.value["last_exit_code"] == 0
    assert read.value["state_file"] is None   # the scheduler keeps none


def test_scheduler_next_run_is_computed_not_read(lake, run):
    read = sources.load_scheduler_status(config_for(lake), ["SPY"], now=NOW)
    assert read.value["next_run"] is None or read.value["next_run"] > NOW


def test_market_status_is_offline_and_deterministic():
    first = sources.market_status(["SPY", "GLD"], now=NOW)
    second = sources.market_status(["SPY", "GLD"], now=NOW)

    assert first.ok
    assert "is_open" in first.value["sessions"].columns
    pd.testing.assert_frame_equal(
        first.value["sessions"], second.value["sessions"]
    )


def test_market_status_without_a_universe():
    assert not sources.market_status([], now=NOW).ok


def test_disk_usage_reports_free_space(tmp_path):
    read = sources.disk_usage(tmp_path)
    assert read.ok
    assert read.value["free_bytes"] > 0
    assert 0.0 <= read.value["free_fraction"] <= 1.0


def test_disk_usage_walks_up_to_an_existing_parent(tmp_path):
    assert sources.disk_usage(tmp_path / "a" / "b" / "c").ok


def test_log_tailing_returns_the_last_lines(lake):
    path = lake / "logs" / "cfdinvest.log"
    path.write_text("\n".join(f"line {n}" for n in range(500)), encoding="utf-8")
    config = AppConfig(paths=PathConfig(data_root=lake))
    object.__setattr__(config.logging, "log_file", str(path))

    read = sources.load_log_files(config)
    assert "application" in read.value


def test_errors_are_filtered_out_of_a_mixed_log(lake):
    path = lake / "logs" / "cfdinvest.log"
    path.write_text(
        "INFO fine\nERROR something broke\nINFO fine again\n", encoding="utf-8"
    )
    config = AppConfig(paths=PathConfig(data_root=lake))
    object.__setattr__(config.logging, "log_file", str(path))

    read = sources.load_error_log(config)
    assert read.value == ["ERROR something broke"]


def test_no_log_file_is_reported_plainly(tmp_path):
    read = sources.load_error_log(config_for(tmp_path))
    assert not read.ok
    assert not read.failed


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #
def healthy_sources(lake: Path, now=NOW) -> dict:
    config = config_for(lake)
    return {
        "config_source": sources.Source(value=config),
        "lake": sources.load_lake_metadata(config, now=now),
        "scheduler_status": sources.load_scheduler_status(config, ["SPY"], now=now),
        "summary": sources.load_last_pipeline_summary(config),
        "execution": sources.load_execution_frame(config),
        "errors": sources.Source(value=[]),
        "disk": sources.disk_usage(lake),
    }


def test_a_healthy_system_shows_no_banner(lake, run):
    report = health.run_checks(**healthy_sources(lake), now=NOW)
    assert not report.show_banner, [c.title for c in report.critical]


def test_every_check_reports_a_status(lake, run):
    report = health.run_checks(**healthy_sources(lake), now=NOW)
    frame = report.to_frame()
    assert len(frame) == len(report.checks)
    assert set(frame["status"]) <= set(health.SEVERITIES)


def test_a_stale_lake_triggers_the_banner(lake, run):
    late = NOW + pd.Timedelta(days=10)
    report = health.run_checks(**healthy_sources(lake, late), now=late)

    assert report.show_banner
    assert "lake_fresh" in {check.key for check in report.critical}


def test_a_failed_run_triggers_the_banner(lake, run):
    payload = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    payload["status"] = "failed"
    payload["error"] = "execution engine exploded"
    (run / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    report = health.run_checks(**healthy_sources(lake), now=NOW)
    keys = {check.key for check in report.critical}

    assert "pipeline_ok" in keys
    assert "execution_ok" in keys   # the error names the execution layer


def test_an_unreadable_config_triggers_the_banner(lake, run):
    inputs = healthy_sources(lake)
    inputs["config_source"] = sources.Source.broken("config.toml is not valid TOML")

    report = health.run_checks(**inputs, now=NOW)
    assert "config" in {check.key for check in report.critical}


def test_an_unreadable_lake_triggers_the_banner(lake, run):
    inputs = healthy_sources(lake)
    inputs["lake"] = sources.Source.broken("permission denied")

    report = health.run_checks(**inputs, now=NOW)
    keys = {check.key for check in report.critical}
    assert "lake_readable" in keys
    assert "lake_fresh" in keys      # freshness is unknowable, not assumed fine


def test_a_full_disk_triggers_the_banner(lake, run):
    inputs = healthy_sources(lake)
    inputs["disk"] = sources.Source(
        value={"free_gb": 0.2, "free_fraction": 0.01, "path": "/",
               "total_bytes": 1, "used_bytes": 1, "free_bytes": 1}
    )
    report = health.run_checks(**inputs, now=NOW)
    assert "disk" in {check.key for check in report.critical}


def test_a_failed_ingestion_triggers_the_banner(lake, run):
    manifest = json.loads(
        (lake / "metadata" / "market_1d_manifest.json").read_text(encoding="utf-8")
    )
    manifest["SPX"]["status"] = "failed"
    (lake / "metadata" / "market_1d_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    report = health.run_checks(**healthy_sources(lake), now=NOW)
    assert "ingestion_ok" in {check.key for check in report.critical}


def test_a_stop_exit_with_no_fill_triggers_the_banner(lake, run):
    from cfdinvest import storage

    trades = storage.read_parquet(run / "trades.parquet")
    trades.loc[0, "reason"] = "stop_loss"
    trades.loc[0, "stop_fill_price"] = np.nan
    storage.write_parquet(trades, run / "trades.parquet")

    report = health.run_checks(**healthy_sources(lake), now=NOW)
    assert "stops_ok" in {check.key for check in report.critical}


def test_a_broken_fx_rate_triggers_the_banner(lake, run):
    from cfdinvest import storage

    trades = storage.read_parquet(run / "trades.parquet")
    trades.loc[0, "fx_rate"] = np.nan
    storage.write_parquet(trades, run / "trades.parquet")

    report = health.run_checks(**healthy_sources(lake), now=NOW)
    assert "fx_ok" in {check.key for check in report.critical}


def test_an_execution_traceback_in_the_log_triggers_the_banner(lake, run):
    inputs = healthy_sources(lake)
    inputs["errors"] = sources.Source(
        value=["2026-08-24 ERROR cfdinvest.execution.engine Traceback ..."]
    )
    report = health.run_checks(**inputs, now=NOW)
    assert "execution_ok" in {check.key for check in report.critical}


def test_a_missed_scheduled_run_triggers_the_banner(lake, run):
    inputs = healthy_sources(lake)
    inputs["scheduler_status"] = sources.Source(
        value={
            "last_run": NOW - pd.Timedelta(hours=3),
            "next_run": NOW - pd.Timedelta(hours=2),
        }
    )
    report = health.run_checks(**inputs, now=NOW)
    assert "missed_run" in {check.key for check in report.critical}


def test_a_check_that_raises_reports_itself_rather_than_crashing():
    """A check that crashes silently stops protecting you."""
    broken = sources.Source(value={"coverage": "not a frame at all"})
    report = health.run_checks(
        config_source=sources.Source(value=None),
        lake=broken,
        scheduler_status=sources.Source(value={}),
        summary=sources.Source(value={}),
        execution=sources.Source(value={}),
        errors=sources.Source(value=[]),
        disk=sources.Source(value={}),
        now=NOW,
    )
    assert isinstance(report.triggered, tuple)   # it produced a report at all


def test_the_banner_text_is_the_specified_string():
    assert health.BANNER_TEXT == (
        "⚠ Critical system warning — results may be unreliable. "
        "See Status page."
    )


def test_thresholds_are_configurable(lake, run):
    strict = health.HealthThresholds(lake_stale_days=0.5)
    report = health.run_checks(**healthy_sources(lake), thresholds=strict, now=NOW)
    assert "lake_fresh" in {check.key for check in report.critical}


# --------------------------------------------------------------------------- #
# Config view
# --------------------------------------------------------------------------- #
def test_flatten_walks_nested_dataclasses():
    frame = configview.to_frame(AppConfig(), AppConfig())
    assert (frame["setting"] == "paths.data_root").any()
    assert not frame["overridden"].any()


def test_an_override_is_detected():
    changed = AppConfig(paths=PathConfig(data_root=Path("/elsewhere")))
    frame = configview.to_frame(changed, AppConfig())
    row = frame[frame["setting"] == "paths.data_root"].iloc[0]

    assert row["overridden"]
    assert "elsewhere" in row["value"]


def test_credentials_are_redacted():
    """This page renders into a browser; a printed key is a leaked key."""
    assert configview.redact("market.api_key", "abcd1234") == "<redacted, 8 chars>"
    assert configview.redact("macro.fred_api_token", "x" * 32).startswith("<redacted")
    assert configview.redact("paths.data_root", "/data") == "/data"


def test_an_empty_secret_is_left_alone():
    assert configview.redact("api_key", None) is None


def test_unknown_config_keys_are_found():
    """Keys the loader silently ignores are the quietest way to be wrong."""
    unknown = configview.unknown_keys(
        {"paths": {"data_root": "/x", "nonsense": 1}, "made_up": True}, AppConfig()
    )
    assert unknown == ["made_up", "paths.nonsense"]


def test_unknown_keys_of_an_empty_payload():
    assert configview.unknown_keys({}, AppConfig()) == []


def test_missing_values_are_flagged():
    frame = configview.to_frame(AppConfig(), AppConfig())
    assert frame["missing"].any()   # log_file is unset by default


def test_long_collections_are_truncated_for_display():
    rendered = configview._display(list(range(50)))
    assert "(+42)" in rendered


# --------------------------------------------------------------------------- #
# The pages actually run
# --------------------------------------------------------------------------- #
PAGE_FILES = ["app.py"] + [
    f"pages/{path.name}" for path in sorted((UI / "pages").glob("*.py"))
]


def render(page: str):
    """Execute one page through Streamlit's own test runner.

    ``AppTest`` runs the script against the real Streamlit API rather than a
    stub, which is the only way to catch the failure that actually happens
    here: an argument Streamlit no longer accepts. A hand-written stub is
    permissive by construction and would pass either way.
    """
    from streamlit.testing.v1 import AppTest

    from cfdinvest.ui import auth

    app = AppTest.from_file(str(UI / page), default_timeout=60)
    # Sign in before running. Every page is gated by page_setup, so without
    # this each of these would assert against a login form -- which is
    # itself worth a test, and is one, below.
    app.session_state[auth.SESSION_KEY] = "tester"
    app.run()
    return app


#: Pages where an st.error is the *product* rather than a failure. The
#: monitoring page drawing a red banner against a stale fixture lake is it
#: working; the simulation page's banner says the figures are not real, and it
#: is deliberately above the numbers rather than below them. Their content is
#: asserted separately, below.
ERROR_IS_CONTENT = {"pages/12_Monitoring.py", "pages/16_Simulation.py"}


def assert_clean(app, page: str) -> None:
    assert not app.exception, (
        f"{page} raised: {[e.value for e in app.exception]}"
    )
    if page in ERROR_IS_CONTENT:
        return
    assert not app.error, f"{page} rendered an error: {[e.value for e in app.error]}"


@pytest.fixture
def populated(lake, run, monkeypatch):
    """Point the whole app at the synthetic lake."""
    monkeypatch.setattr(
        sources, "load_app_config",
        lambda *a, **k: sources.Source(value=config_for(lake)),
    )
    return lake


@pytest.fixture
def empty_lake(tmp_path, monkeypatch):
    """A lake directory that exists but holds nothing — a fresh install."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(
        sources, "load_app_config",
        lambda *a, **k: sources.Source(value=config_for(root)),
    )
    return root


@pytest.fixture
def absent_lake(tmp_path, monkeypatch):
    """No lake at all — a misconfigured CFD_DATA_ROOT."""
    monkeypatch.setattr(
        sources, "load_app_config",
        lambda *a, **k: sources.Source(value=config_for(tmp_path / "gone")),
    )
    return tmp_path / "gone"


@pytest.mark.parametrize("page", PAGE_FILES)
def test_every_page_renders_against_a_populated_lake(page, populated):
    app = render(page)
    assert_clean(app, page)
    assert app.markdown, f"{page} drew nothing"


@pytest.mark.parametrize("page", PAGE_FILES)
def test_every_page_renders_against_an_empty_lake(page, empty_lake):
    """The state a fresh install is in, and the one most likely to crash."""
    app = render(page)
    assert not app.exception, (
        f"{page} raised: {[e.value for e in app.exception]}"
    )
    assert app.markdown, f"{page} drew nothing"


@pytest.mark.parametrize("page", PAGE_FILES)
def test_every_page_renders_when_the_lake_is_missing_entirely(page, absent_lake):
    app = render(page)
    assert not app.exception, (
        f"{page} raised: {[e.value for e in app.exception]}"
    )
    assert app.markdown, f"{page} drew nothing"


def test_navigation_labels_match_the_page_headings():
    """A nav entry that does not match the page it opens is a papercut."""
    import ast

    entry = ast.parse((UI / "app.py").read_text(encoding="utf-8"))
    declared = {}
    for node in ast.walk(entry):
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", None
        ) == "PAGES":
            for element in node.value.elts:
                path, title, _icon = (item.value for item in element.elts)
                declared[path] = title

    assert declared, "app.py no longer declares PAGES"

    for relative, title in declared.items():
        source = (UI / relative).read_text(encoding="utf-8")
        assert f'"{title}"' in source, (
            f"{relative} does not print the title {title!r} the nav gives it"
        )


def test_every_page_file_appears_in_the_navigation():
    import ast

    entry = ast.parse((UI / "app.py").read_text(encoding="utf-8"))
    declared = set()
    for node in ast.walk(entry):
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", None
        ) == "PAGES":
            declared = {element.elts[0].value for element in node.value.elts}

    on_disk = {f"pages/{p.name}" for p in (UI / "pages").glob("*.py")}
    assert declared == on_disk


def test_the_pages_directory_matches_the_specified_layout():
    expected = {
        "00_Overview.py", "01_Status.py", "02_Signals.py", "03_Execution.py",
        "04_Positions.py", "05_Profit_and_Loss.py", "06_Logs.py",
        "07_Configuration.py", "08_Analytics.py", "09_Live_Helper.py",
        "10_Risk.py",
        "11_Confirm_Trades.py",
        "12_Monitoring.py",
        "13_Journal.py",
        "14_Insights.py",
        "15_Behaviour.py",
        "16_Simulation.py",
        "17_Accounts.py",
        "18_Routing.py",
        "19_T212_Rules.py",
        "20_Meta_Strategy.py",
        "21_Account.py",
    }
    assert {p.name for p in (UI / "pages").glob("*.py")} == expected


def test_the_banner_is_on_every_page(populated, monkeypatch):
    """It is a *persistent* banner: absent from one page is a hole."""
    monkeypatch.setattr(
        health, "run_checks",
        lambda **kwargs: health.HealthReport(
            checks=(
                health.Check(
                    key="x", title="Forced failure", triggered=True,
                    detail="injected by the test",
                ),
            )
        ),
    )
    for page in PAGE_FILES:
        drawn = " ".join(element.value for element in render(page).markdown)
        assert health.BANNER_TEXT in drawn, f"{page} drew no banner"


def test_a_healthy_system_draws_no_banner(populated, monkeypatch):
    monkeypatch.setattr(
        health, "run_checks", lambda **kwargs: health.HealthReport(checks=())
    )
    drawn = " ".join(element.value for element in render("app.py").markdown)
    assert health.BANNER_TEXT not in drawn
    assert "All health checks passing" in drawn


def test_the_read_only_badge_is_on_every_page(populated):
    for page in PAGE_FILES:
        drawn = " ".join(element.value for element in render(page).markdown)
        assert "cfd-readonly" in drawn, f"{page} does not say it is read-only"


def test_the_status_page_expands_every_failing_check(populated, monkeypatch):
    monkeypatch.setattr(
        health, "run_checks",
        lambda **kwargs: health.HealthReport(
            checks=(
                health.Check(
                    key="x", title="Forced failure", triggered=True,
                    detail="what was found", remedy="what to do",
                ),
            )
        ),
    )
    app = render("pages/01_status.py")
    drawn = " ".join(element.value for element in app.markdown)
    assert "what was found" in drawn
    assert "what to do" in drawn


# --------------------------------------------------------------------------- #
# The monitoring page reports rather than breaks
# --------------------------------------------------------------------------- #
def test_the_monitoring_page_reports_a_stale_lake(populated):
    """The fixture lake is stale, so the page should say so, loudly.

    This is the page whose st.error means it is working. If it ever renders
    green against this fixture, the checks have stopped checking.
    """
    app = render("pages/12_Monitoring.py")

    assert not app.exception
    drawn = " ".join(element.value for element in app.error)
    assert "lake" in drawn.lower()
    assert "RED" in " ".join(element.value for element in app.markdown)


def test_the_monitoring_page_shows_every_check_not_only_the_failures(populated):
    """A summary of only what broke cannot tell you what was looked at."""
    from cfdinvest.monitoring.aggregate import TYPE_LABELS

    app = render("pages/12_Monitoring.py")
    labels = {metric.label for metric in app.metric}

    assert set(TYPE_LABELS.values()) <= labels


def test_the_monitoring_page_survives_an_empty_lake(empty_lake):
    """The state a fresh install is in, and the one most likely to crash."""
    app = render("pages/12_Monitoring.py")
    assert not app.exception
    assert app.markdown


def test_opening_the_monitoring_page_records_nothing(populated):
    """A dashboard left open would otherwise bury the scheduler's passes."""
    from cfdinvest.persist import monitoring as monitoring_store

    render("pages/12_Monitoring.py")
    assert monitoring_store.list_monitoring_snapshots(config_for(populated)) == []


# --------------------------------------------------------------------------- #
# The simulation page says what it is before it says anything else
# --------------------------------------------------------------------------- #
def test_the_simulation_page_warns_before_it_shows_a_number(populated):
    """A banner underneath is one people scroll past on the way to the figure
    they came for."""
    app = render("pages/16_Simulation.py")

    assert not app.exception
    drawn = " ".join(element.value for element in app.error)
    assert "rehearsal" in drawn.lower()
    assert "not your results" in drawn.lower()


def test_the_simulation_page_rehearses_nothing_until_asked(populated):
    """Replaying a run is expensive, and doing it on page load would make the
    dashboard slow for everyone who was not asking for it."""
    app = render("pages/16_Simulation.py")
    drawn = " ".join(element.value for element in app.markdown)

    assert "Nothing has been rehearsed yet" in drawn


def test_the_simulation_page_records_nothing_by_opening_it(populated):
    from cfdinvest.persist import simulation as simulation_store

    render("pages/16_Simulation.py")
    assert simulation_store.list_simulations(config_for(populated)) == []


# --------------------------------------------------------------------------- #
# The risk page shows what the snapshot says
# --------------------------------------------------------------------------- #
def risk_snapshot(**changes):
    """A snapshot with a valued book, for pinning what the page renders."""
    import pandas as pd

    from cfdinvest.risk import RiskLimits, build_risk_snapshot

    index = pd.bdate_range("2026-01-01", periods=2, tz="UTC", name="timestamp")
    arguments = dict(
        positions_frame=pd.DataFrame({"SPY": [0.0, 10.0], "GLD": [0.0, -5.0]}, index=index),
        analysis_frame=pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-01-02"] * 2, utc=True),
                "panel": ["prices.close"] * 2,
                "series": ["SPY", "GLD"],
                "value": [100.0, 80.0],
            }
        ),
        limits=RiskLimits(
            account_balance_gbp=2_000.0,
            # Two positions are 71% concentrated by construction. This fixture
            # is the inside-every-limit case, so the bound has to allow it.
            max_instrument_concentration_pct=0.90,
        ),
        scheduler_status={"run_status": "OK", "message": "Last run completed."},
        run_id="20260102-090000",
    )
    arguments.update(changes)
    return build_risk_snapshot(**arguments)


def render_risk(monkeypatch, snapshot):
    """Render the risk page against one known snapshot.

    The page reads the artefacts itself, so the fixture goes in at the read:
    what is under test is what the page *does* with a snapshot, not whether
    the snapshot is right -- tests/test_risk.py covers that.
    """
    import cfdinvest.risk as risk_package

    monkeypatch.setattr(
        risk_package, "snapshot_from_disk", lambda *a, **k: snapshot
    )
    return render("pages/10_Risk.py")


def test_the_risk_page_shows_the_exposure_it_was_given(populated, monkeypatch):
    app = render_risk(monkeypatch, risk_snapshot())
    assert_clean(app, "pages/10_Risk.py")

    shown = {metric.label: metric.value for metric in app.metric}
    # 10 x 100 long and 5 x 80 short: 1,400 gross, 600 net, two positions.
    assert shown["Gross exposure"] == "£1,400.00"
    assert shown["Net exposure"] == "£600.00"
    assert shown["Open positions"] == "2"


def test_the_risk_page_lists_every_breach(populated, monkeypatch):
    from cfdinvest.risk import RiskLimits

    app = render_risk(
        monkeypatch,
        risk_snapshot(
            limits=RiskLimits(
                account_balance_gbp=2_000.0, max_total_exposure_gbp=500.0
            )
        ),
    )
    drawn = " ".join(element.value for element in app.error)

    assert "limit" in drawn and "breached" in drawn
    assert "Gross exposure" in drawn


def test_a_flat_book_is_not_reported_as_a_breach(populated, monkeypatch):
    import pandas as pd

    index = pd.bdate_range("2026-01-01", periods=2, tz="UTC", name="timestamp")
    app = render_risk(
        monkeypatch,
        risk_snapshot(
            positions_frame=pd.DataFrame({"SPY": [0.0, 0.0]}, index=index),
            analysis_frame=None,
        ),
    )
    assert not app.error
    assert "flat" in " ".join(element.value for element in app.info).lower()


def test_the_risk_page_shows_a_dead_run_as_failed(populated, monkeypatch):
    """The state the scheduler ordering exists to expose."""
    app = render_risk(
        monkeypatch,
        risk_snapshot(
            scheduler_status={
                "run_status": "FAILED",
                "message": "Last run started but did not complete.",
                "failures": 1,
            }
        ),
    )
    drawn = " ".join(element.value for element in app.markdown)

    assert "FAILED" in drawn
    assert "did not complete" in drawn


def test_the_risk_page_offers_no_control_that_changes_anything(
    populated, monkeypatch
):
    """It is a dashboard. Nothing on it may close, size or place a trade.

    The shared sidebar's "Refresh data" re-reads the lake, so the assertion
    is not "no buttons" but "no button that acts on the book". Sign out is
    drawn by the entry script rather than by a page, so it does not appear
    when a page is rendered on its own. Listing the chrome explicitly rather
    than loosening the check: a new button here should fail this test until
    someone has looked at it.
    """
    app = render_risk(monkeypatch, risk_snapshot())
    labels = [button.label for button in app.button]

    assert labels == ["Refresh data"]
    forbidden = ("close", "sell", "buy", "place", "reduce", "hedge", "size")
    assert not [
        label for label in labels
        if any(word in label.lower() for word in forbidden)
    ]


# --------------------------------------------------------------------------- #
# Plain English
# --------------------------------------------------------------------------- #
def test_a_reason_becomes_a_sentence():
    """The format the signal layer actually emits: rule|flag @regime."""
    assert explain.reason("trend_short|high_volatility @crisis") == (
        "Downtrend, and the filters allow a short position — volatility is "
        "high, so the size was cut. Market regime: Crisis."
    )


def test_a_reason_without_flags_or_regime():
    assert explain.reason("trend_long") == (
        "Uptrend, and the filters allow a long position."
    )


def test_a_reason_with_a_regime_but_no_flags():
    assert explain.reason("trend_long @risk_on").endswith(
        "Market regime: Risk-on."
    )


def test_several_flags_read_as_a_list():
    sentence = explain.reason(
        "blocked_long|high_volatility|correlation_breakdown @risk_off"
    )
    assert "volatility is high, so the size was cut, and correlations have "
    "broken down" in sentence
    assert sentence.startswith("An uptrend, but the regime filters block")


def test_an_unknown_token_is_shown_not_dropped():
    """A new rule upstream must not silently vanish from the explanation."""
    sentence = explain.reason("some_new_rule|some_new_flag @some_new_regime")
    assert "some new rule" in sentence
    assert "some new flag" in sentence
    assert "some new regime" in sentence


def test_an_empty_reason_says_so():
    assert explain.reason("") == "No reason recorded."
    assert explain.reason(None) == "No reason recorded."


@pytest.mark.parametrize("rule", list(explain.RULES))
def test_every_rule_has_a_sentence(rule):
    assert explain.RULES[rule]
    assert "_" not in explain.RULES[rule]


@pytest.mark.parametrize("flag", list(explain.FLAGS))
def test_every_flag_has_a_clause(flag):
    assert explain.FLAGS[flag]
    assert "_" not in explain.FLAGS[flag]


def test_the_vocabulary_matches_the_pipeline():
    """If the signal layer grows a rule, this test is how we find out."""
    from cfdinvest.regimes.combined import COMBINED_REGIMES
    from cfdinvest.signals.combined import DIRECTION_PRECEDENCE
    from cfdinvest.signals.filters import REASON_FLAGS

    assert {rule.name for rule in DIRECTION_PRECEDENCE} <= set(explain.RULES)
    assert set(REASON_FLAGS) <= set(explain.FLAGS)
    assert set(COMBINED_REGIMES) <= set(explain.REGIMES)


def test_every_execution_action_is_translated():
    from cfdinvest.execution.state import ACTIONS

    assert set(ACTIONS) <= set(explain.ACTIONS)


def test_actions_and_outcomes_read_as_english():
    assert explain.action("flip_long_to_short") == "Flip from long to short"
    assert explain.action("stop_loss") == "Stopped out at the stop-loss level"
    assert explain.outcome("ok") == "Succeeded"
    assert explain.outcome("failed") == "Failed"


def test_a_breach_explains_what_happened_to_the_level():
    assert "gapped past" in explain.breach("gap")
    assert "traded through" in explain.breach("intrabar")
    assert explain.breach("") == ""


# --------------------------------------------------------------------------- #
# The brand mark
# --------------------------------------------------------------------------- #
def test_the_wordmark_and_strapline_are_the_brand_ones():
    assert theme.BRAND["name"] == "CFDINVEST"
    assert theme.BRAND["strapline"] == "Trend-following CFD research."


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_wordmark_colour_is_the_same_in_both_modes(mode):
    """A wordmark that changes colour between modes is not the wordmark."""
    assert theme.BRAND["colour"] in theme.brand_svg(mode)


@pytest.mark.parametrize("mode", theme.MODES)
def test_the_wordmark_renders_as_an_svg(mode):
    """An image, because the logo slot is the only place above the nav."""
    svg = theme.brand_svg(mode)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert theme.BRAND["name"] in svg
    assert theme.BRAND["strapline"] in svg
    assert theme.BRAND["colour"] in svg


def test_the_strapline_follows_the_palette():
    """Grey on black and grey on white are not the same grey."""
    assert theme.palette_for("dark")["subtle"] in theme.brand_svg("dark")
    assert theme.palette_for("light")["subtle"] in theme.brand_svg("light")


# --------------------------------------------------------------------------- #
# Scaling a simulated position onto a real account
# --------------------------------------------------------------------------- #
def bundle(initial_equity=100_000.0) -> dict:
    return {
        "summary": sources.Source(
            value={"metrics": {"initial_equity": initial_equity}}
        )
    }


def test_the_scale_factor_is_the_ratio_of_the_two_accounts():
    """A £2,000 account trading a £100,000 backtest holds one fiftieth."""
    assert components.scale_factor(bundle(100_000.0), 2_000.0) == pytest.approx(0.02)


def test_scaling_is_the_identity_at_the_simulated_size():
    assert components.scale_factor(bundle(2_000.0), 2_000.0) == pytest.approx(1.0)


def test_an_unknown_starting_equity_does_not_scale():
    """Unscaled is honestly the simulator's; a guessed factor is nobody's."""
    assert components.scale_factor({"summary": sources.Source(value={})}, 2_000.0) == 1.0
    assert components.scale_factor(bundle(0.0), 2_000.0) == 1.0


def test_a_zero_balance_does_not_scale():
    assert components.scale_factor(bundle(), 0.0) == 1.0
    assert components.scale_factor(bundle(), None) == 1.0


def test_the_default_balance_is_the_virtual_account():
    assert components.DEFAULT_BALANCE == pytest.approx(2_000.0)


def test_the_simulated_equity_is_read_from_the_run():
    assert components.simulated_equity(bundle(50_000.0)) == pytest.approx(50_000.0)


# --------------------------------------------------------------------------- #
# Table formatting
# --------------------------------------------------------------------------- #
def test_column_headings_spell_out_abbreviations():
    assert components._label("realized_pnl") == "Realised profit & loss"
    assert components._label("fx_conversion_fee") == "Currency conversion fee"
    assert components._label("stop_level") == "Stop-loss level"
    assert components._label("asset") == "Instrument"


def test_an_unmapped_column_is_still_readable():
    assert components._label("some_new_column") == "Some new column"


def test_money_columns_are_detected_from_their_names():
    assert components._format_for("spread_cost") == "money"
    assert components._format_for("realized_pnl") == "money"
    assert components._format_for("fill_price") == "price"
    assert components._format_for("fx_rate") == "rate"
    assert components._format_for("confidence") == "percent"
    assert components._format_for("total_return") == "percent"


def test_display_moves_timestamps_into_the_operators_zone():
    frame = pd.DataFrame(
        {"when": pd.to_datetime(["2026-08-24T09:30:00Z"])},
        index=pd.DatetimeIndex(["2026-08-24T09:30:00Z"], name="timestamp"),
    )
    shown = components.for_display(frame)

    assert str(shown["when"].dt.tz) == "Europe/London"
    assert shown["when"].iloc[0].hour == 10
    assert shown.index[0].hour == 10


def test_display_localises_a_naive_index():
    frame = pd.DataFrame(
        {"x": [1]}, index=pd.DatetimeIndex(["2026-08-24T09:30:00"])
    )
    assert components.for_display(frame).index.tz is not None


def test_display_of_an_empty_frame_is_harmless():
    empty = pd.DataFrame()
    assert components.for_display(empty).empty


# --------------------------------------------------------------------------- #
# The read-only contract
# --------------------------------------------------------------------------- #
#: Functions no module in this package may ever call, and modules it may
#: never import. Checked against the parsed syntax tree rather than the raw
#: text, so a log filename that happens to contain "ingest" is not a
#: violation and a genuine call cannot hide inside a docstring.
FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        # Persisting anything, on any receiver. These names are unambiguous:
        # nothing in a read-only dashboard has a reason to say them.
        "write_parquet", "write_json", "upsert_parquet", "to_parquet",
        "to_csv", "to_json", "write_text", "write_bytes", "mkdir", "rmdir",
        "unlink", "touch", "rmtree",
        # Running the pipeline, or trading.
        "run_pipeline", "run_backtest", "run_execution", "ingest_market",
        "ingest_macro",
        # Reaching outside.
        "urlopen", "urlretrieve",
    }
)

#: Verbs that are only dangerous on certain receivers. ``replace`` is a
#: Series method far more often than it is ``os.replace``, and ``get`` is a
#: dict method far more often than it is an HTTP verb, so banning the bare
#: name would ban ordinary pandas.
FORBIDDEN_METHODS: dict[str, frozenset[str]] = {
    "os": frozenset({"remove", "rename", "replace", "unlink", "rmdir",
                     "makedirs", "mkdir", "system"}),
    "shutil": frozenset({"rmtree", "move", "copy", "copytree"}),
    "subprocess": frozenset({"run", "call", "Popen", "check_output"}),
    "requests": frozenset({"get", "post", "put", "delete", "request",
                           "Session"}),
    "session": frozenset({"get", "post", "put", "delete", "request"}),
    "storage": frozenset({"write_parquet", "write_json", "upsert_parquet"}),
}

#: Modules that would let a page reach the network, a vendor or a shell.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "requests", "urllib", "urllib3", "httpx", "aiohttp", "socket",
        "yfinance", "smtplib", "subprocess", "multiprocessing",
        "cfdinvest.ingestion", "cfdinvest.schedule.notify",
    }
)


def _calls_and_imports(path: Path):
    """Every called name and imported module in one file."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[tuple[str, str]] = []
    imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                receiver = (
                    function.value.id
                    if isinstance(function.value, ast.Name)
                    else type(function.value).__name__
                )
                calls.append((receiver.lower(), function.attr))
            elif isinstance(function, ast.Name):
                calls.append(("", function.id))
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    return calls, imports


@pytest.mark.parametrize("path", sorted(UI.rglob("*.py")))
def test_no_ui_module_can_write_trade_or_fetch(path):
    """The safety contract, enforced rather than promised.

    The whole point of this package is that it cannot do anything. Checking
    the syntax tree is what keeps this true when somebody adds a page in six
    months without reading the docstring at the top of ``sources.py``.
    """
    calls, imports = _calls_and_imports(path)

    for receiver, name in calls:
        assert name not in FORBIDDEN_CALLS, f"{path.name} calls {name}()"
        assert name not in FORBIDDEN_METHODS.get(receiver, frozenset()), (
            f"{path.name} calls {receiver}.{name}()"
        )

    for module in imports:
        root = module.split(".")[0]
        assert module not in FORBIDDEN_IMPORTS and root not in FORBIDDEN_IMPORTS, (
            f"{path.name} imports {module}"
        )


@pytest.mark.parametrize("path", sorted(UI.rglob("*.py")))
def test_no_ui_module_opens_a_file_for_writing(path):
    source = path.read_text(encoding="utf-8")
    for mode in ('"w"', "'w'", '"a"', "'a'", '"wb"', "'wb'", '"w+"', "'w+'"):
        assert f".open({mode}" not in source, path.name
        assert f"open(path, {mode}" not in source, path.name


def test_the_read_only_contract_is_stated_where_it_is_enforced():
    """The claim and the code that keeps it true live in the same file."""
    text = (UI / "sources.py").read_text(encoding="utf-8")
    assert "never writes" in text or "Nothing in this module writes" in text


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def test_a_count_of_one_reads_as_one():
    """"1 days old" is small, and it makes the reader trust the number less."""
    assert components.plural(1, "day") == "1 day"
    assert components.plural(2, "day") == "2 days"
    assert components.plural(0, "day") == "0 days"
    assert components.plural(1.4, "day") == "1 day"
    assert components.plural(None, "day") == components.DASH


def test_the_application_title_has_one_owner():
    """The tab said "cfdinvest" in two places, spelled out separately.

    The wordmark in the logo slot is a different string with a different job,
    and it stays as it is -- this is only about what the browser tab says.
    """
    from cfdinvest.ui import theme

    assert theme.APP_TITLE == "CFD Invest"

    sources_text = (UI / "app.py").read_text(encoding="utf-8")
    components_text = (UI / "components.py").read_text(encoding="utf-8")
    for name, text in (("app.py", sources_text), ("components.py", components_text)):
        assert "page_title=\"cfdinvest\"" not in text, name
        assert "· cfdinvest" not in text, name
        assert "APP_TITLE" in text, f"{name} should read the shared title"


def test_the_overview_and_status_pages_agree_on_what_the_lake_facts_are_called():
    """The same number under two names is a papercut, and one name was wrong.

    ``series_count`` is market *plus* macro, so labelling it "Instruments
    stored" reported 37 instruments for a lake holding 25 of them.
    """
    overview = (UI / "pages" / "00_Overview.py").read_text(encoding="utf-8")
    status = (UI / "pages" / "01_Status.py").read_text(encoding="utf-8")

    assert "Instruments stored" not in overview
    for label in ("Newest bar", "Instruments", "Macro series",
                  "Data quality flags"):
        assert label in overview, label
        assert label in status, label


def test_a_clock_reading_is_one_fixed_width():
    """The cards need every timestamp the same shape, and in local time."""
    text = components.clock("2026-08-25T16:42:03+00:00")

    assert re.fullmatch(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", text), text
    assert text.startswith("25/08/2026")
    assert components.clock(None) == components.DASH
    assert components.clock("not a date") == components.DASH


def test_the_manifest_is_not_shown_as_python():
    """A table of dict reprs tells a reader nothing and looks like a leak."""
    frame = pd.DataFrame(
        {
            "status": ["ok", "ok"],
            "rows_total": [10, 20],
            "first_timestamp": ["2018-01-02", "2018-01-02"],
            "last_timestamp": ["2026-08-24", "2026-08-24"],
            "anomalies": [{"close_outside_range": 88}, {}],
            "error": [None, None],
            "params": [{"interval": "1d", "auto_adjust": True}, {}],
            "updated_at": ["2026-08-25T12:15:14+00:00"] * 2,
        },
        index=["AUDUSD=X", "DBC"],
    )

    shown = components.coverage_table(frame)

    # The provenance blob is gone, and the columns read as English.
    assert "params" not in shown.columns and "Params" not in shown.columns
    assert list(shown.columns)[:2] == ["Status", "Rows"]

    flags = shown["Data quality flags"].tolist()
    assert flags[0] == "close outside range: 88", flags
    assert "{" not in flags[0] and "_" not in flags[0]
    # An empty mapping is the ordinary case and must read as nothing at all.
    assert flags[1] == components.DASH

    # "None" down an Error column reads as a fault rather than the absence.
    assert set(shown["Error"]) == {components.DASH}


def test_describe_flags_leaves_a_string_alone():
    """It runs over columns that may already have been formatted."""
    assert components.describe_flags("already text") == "already text"
    assert components.describe_flags({}) == components.DASH
    assert components.describe_flags(None) == components.DASH


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
def test_a_state_column_is_coloured_and_a_number_is_not():
    """Reading "ok" down 25 rows to find the "failed" is work a colour does.

    Numbers are deliberately never tinted: the reader would have to guess
    what threshold the colour was claiming.
    """
    frame = pd.DataFrame(
        {"status": ["ok", "failed", "brand new"], "rows": [1, 2, 3]}
    )

    html_out = components.style_states(frame, "dark").to_html()
    painted = re.findall(r"_row(\d)_col(\d)[^{]*\{([^}]*)\}", html_out)
    cells = {(int(r), int(c)): css for r, c, css in painted}

    assert theme.colour_for("ok", "dark") in cells[(0, 0)]
    assert theme.colour_for("critical", "dark") in cells[(1, 0)]
    # An unrecognised state gets no colour rather than an invented one.
    assert (2, 0) not in cells
    # And nothing in the numeric column is painted at all.
    assert not any(col == 1 for _, col in cells)


def test_not_checked_is_not_colour_coded_as_fine():
    """The project's rule about silence, in colour.

    "Not measured" and "measured and fine" are different statements, and
    only one of them is green.
    """
    for word in ("unchecked", "unverified", "not checked"):
        assert components.state_colour(word, "dark") == theme.colour_for(
            "warning", "dark"
        ), word
        assert components.state_colour(word, "dark") != theme.colour_for(
            "ok", "dark"
        ), word


def test_a_failed_outcome_does_not_look_like_a_successful_one():
    """Streamlit paints every metric value the same accent colour."""
    succeeded = components.state_colour("Succeeded", "dark")
    failed = components.state_colour("Failed", "dark")

    assert succeeded == theme.colour_for("ok", "dark")
    assert failed == theme.colour_for("critical", "dark")
    assert succeeded != failed


def test_colouring_never_takes_a_table_down():
    """Colour is a nicety; the table is not."""
    class Awkward(pd.DataFrame):
        @property
        def style(self):  # noqa: D401
            raise RuntimeError("no styling here")

    frame = Awkward({"status": ["ok"]})
    assert components.style_states(frame, "dark") is frame


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #
def auth_config(tmp_path):
    from cfdinvest.config import AppConfig, PathConfig

    return AppConfig(paths=PathConfig(data_root=tmp_path))


def test_a_password_is_never_stored_in_the_clear(tmp_path):
    """The file must not contain the password under any key."""
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials(
        {"users": {"admin": auth.hash_password("correct horse battery")}}, config
    )

    body = auth.credentials_path(config).read_text(encoding="utf-8")
    assert "correct horse battery" not in body
    assert "scrypt" in body
    assert auth.check_login("admin", "correct horse battery", config)
    assert not auth.check_login("admin", "wrong", config)


def test_two_identical_passwords_hash_differently(tmp_path):
    """Salted per credential, so one cracked hash is not two."""
    from cfdinvest.ui import auth

    first = auth.hash_password("the same password")
    second = auth.hash_password("the same password")

    assert first["salt"] != second["salt"]
    assert first["hash"] != second["hash"]
    assert auth.verify_password("the same password", first)
    assert auth.verify_password("the same password", second)


def test_an_unknown_user_is_refused_without_saying_so(tmp_path):
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("x" * 14)}}, config)

    assert not auth.check_login("nobody", "x" * 14, config)
    assert not auth.check_login("", "", config)


def test_changing_a_password_requires_the_current_one(tmp_path):
    """A session left open must not be enough to lock the owner out."""
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("old password 1")}},
                          config)

    changed, message = auth.change_password(
        "admin", "not the password", "brand new password", "brand new password",
        config,
    )
    assert not changed and "current password" in message
    assert auth.check_login("admin", "old password 1", config)

    changed, _ = auth.change_password(
        "admin", "old password 1", "brand new password", "brand new password",
        config,
    )
    assert changed
    assert auth.check_login("admin", "brand new password", config)
    assert not auth.check_login("admin", "old password 1", config)


@pytest.mark.parametrize(
    "new, confirm, fragment",
    [("short", "short", "12 characters"),
     ("a long enough one", "a different one", "do not match"),
     ("old password 1", "old password 1", "already using")],
    ids=["too short", "mismatched", "unchanged"],
)
def test_a_bad_new_password_is_refused_with_a_reason(tmp_path, new, confirm, fragment):
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("old password 1")}},
                          config)

    changed, message = auth.change_password(
        "admin", "old password 1", new, confirm, config
    )

    assert not changed
    assert fragment in message
    # And the old one still works: a refused change must change nothing.
    assert auth.check_login("admin", "old password 1", config)


def test_the_environment_seeds_an_account_but_never_resets_one(tmp_path, monkeypatch):
    """Otherwise every restart would undo a password the owner had changed."""
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    monkeypatch.setenv("CFD_DASHBOARD_PASSWORD", "seeded password one")

    assert auth.bootstrap_from_environment(config)
    assert auth.check_login("admin", "seeded password one", config)

    auth.change_password("admin", "seeded password one", "chosen by me now",
                         "chosen by me now", config)

    # Second bootstrap must be a no-op even though the variable still says so.
    assert not auth.bootstrap_from_environment(config)
    assert auth.check_login("admin", "chosen by me now", config)
    assert not auth.check_login("admin", "seeded password one", config)


def test_no_account_is_created_without_a_password_in_the_environment(
    tmp_path, monkeypatch
):
    """There is no sign-up screen on purpose.

    An unclaimed dashboard on a public address belongs to whoever finds it
    first, so an unconfigured deployment refuses everyone rather than
    offering to enrol the first visitor.
    """
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    monkeypatch.delenv("CFD_DASHBOARD_PASSWORD", raising=False)

    assert not auth.bootstrap_from_environment(config)
    assert not auth.is_configured(config)
    assert not auth.check_login("admin", "anything", config)


def _anonymous_overview(monkeypatch, populated):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(UI / "pages" / "00_Overview.py"), default_timeout=60)
    app.run()
    return app


def test_an_anonymous_visitor_gets_no_page_content(monkeypatch, populated):
    """The gate must withhold the page, not merely decorate it."""
    monkeypatch.setenv("CFD_DASHBOARD_PASSWORD", "a seeded password")

    app = _anonymous_overview(monkeypatch, populated)

    rendered = " ".join(
        [block.value for block in app.markdown]
        + [header.value for header in app.subheader]
        + [title.value for title in app.title]
    )
    for heading in ("Open positions", "Last pipeline run", "Next scheduled run"):
        assert heading not in rendered, heading
    # The sign-in form is what is offered instead.
    assert any("Sign in" in header.value for header in app.subheader)


def test_an_unconfigured_dashboard_refuses_everyone(monkeypatch, populated):
    """No sign-up screen: an unclaimed dashboard belongs to whoever finds it."""
    monkeypatch.delenv("CFD_DASHBOARD_PASSWORD", raising=False)
    # The fixture lake is shared, so an account seeded by an earlier test
    # would still be there. This test is about the unconfigured state.
    from cfdinvest.ui import auth

    store = auth.credentials_path()
    if store.exists():
        store.unlink()

    app = _anonymous_overview(monkeypatch, populated)

    errors = " ".join(block.value for block in app.error)
    assert "No dashboard account exists" in errors
    assert not any("Sign in" in header.value for header in app.subheader)


def test_a_session_survives_a_refresh(tmp_path):
    """The bug a refresh found.

    Streamlit's session_state lives for one websocket session, so a browser
    refresh starts a new one and signed you out. The session is carried in a
    signed cookie instead; session_state is a per-run cache of it.
    """
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("x" * 14)}}, config)

    token = auth.issue_token("admin", config)
    assert auth.verify_token(token, config) == "admin"

    # The secret is kept, so a restart does not sign everybody out.
    reread = auth.session_secret(config)
    assert reread == auth.session_secret(config)
    assert auth.verify_token(token, config) == "admin"


def test_a_session_cookie_cannot_be_edited_to_last_longer(tmp_path):
    """The signature covers the expiry, so the browser holds nothing it can
    usefully change."""
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("x" * 14)}}, config)

    name, expires, signature = auth.issue_token("admin", config).rsplit("|", 2)
    forged = f"{name}|{int(expires) + 86400 * 365}|{signature}"

    assert auth.verify_token(forged, config) is None
    assert auth.verify_token(f"admin|{expires}|{'0' * 64}", config) is None
    assert auth.verify_token("not a token", config) is None


def test_an_expired_or_deleted_session_stops_working(tmp_path):
    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    auth.save_credentials({"users": {"admin": auth.hash_password("x" * 14)}}, config)
    token = auth.issue_token("admin", config)

    assert auth.verify_token(token, config, now=9e9) is None

    # A token for an account that no longer exists must not still open it.
    document = auth.load_credentials(config)
    document["users"] = {}
    auth.save_credentials(document, config)
    assert auth.verify_token(token, config) is None


def test_the_entry_script_gates_before_it_draws_the_navigation():
    """The other half of what a refresh showed: the page list leaked.

    st.navigation draws the sidebar as soon as it is called, so gating only
    inside the pages showed an anonymous visitor every page name and then
    refused each one.
    """
    body = (UI / "app.py").read_text(encoding="utf-8")

    gate = body.index("components.require_login(st)")
    navigation = body.index("st.navigation(")
    assert gate < navigation, "the gate must come before the navigation"
