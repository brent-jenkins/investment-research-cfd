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
    f"views/{path.name}" for path in sorted((UI / "views").glob("*.py"))
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
ERROR_IS_CONTENT = {"views/12_Monitoring.py", "views/16_Simulation.py"}


def assert_clean(app, page: str) -> None:
    assert not app.exception, (
        f"{page} raised: {[e.value for e in app.exception]}"
    )
    if page in ERROR_IS_CONTENT:
        return
    assert not app.error, f"{page} rendered an error: {[e.value for e in app.error]}"


@pytest.fixture
def populated(lake, run, monkeypatch):
    """Point the whole app at the synthetic lake.

    The bundle cache is cleared first. ``read_everything`` memoises on a key
    that does not vary between tests, so without this a test inherits the
    previous one's sources -- which made a test asserting on absent live
    quotes pass against the previous test's present ones, and pass or fail
    depending on the order pytest happened to run them in.
    """
    import streamlit as _st

    try:
        _st.cache_data.clear()
    except Exception:  # noqa: BLE001 - clearing must never fail a test
        pass

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

    on_disk = {f"views/{p.name}" for p in (UI / "views").glob("*.py")}
    assert declared == on_disk


def test_the_pages_directory_matches_the_specified_layout():
    """Three pages, deliberately.

    The dashboard carried twenty-three during development and almost none
    of them were read. This is a research period: the questions that matter
    are what the paper book has done, whether the machine is healthy, and
    what the model wants today. Everything else was removed rather than
    left to rot behind a nav item, and git holds it if it is wanted back.
    """
    expected = {"00_Dashboard.py", "01_Status.py", "02_Signals.py"}
    assert {p.name for p in (UI / "views").glob("*.py")} == expected


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
    app = render("views/01_status.py")
    drawn = " ".join(element.value for element in app.markdown)
    assert "what was found" in drawn
    assert "what to do" in drawn


# --------------------------------------------------------------------------- #
# The monitoring page reports rather than breaks
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
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


def test_a_clock_reading_is_one_fixed_width():
    """The cards need every timestamp the same shape, and in local time.

    Minutes, not seconds. Nothing on these pages is read to the second, and
    the extra three characters pushed the tiles wider than their column --
    precision that costs layout and buys nothing.
    """
    text = components.clock("2026-08-25T16:42:03+00:00")

    assert re.fullmatch(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}", text), text
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

    app = AppTest.from_file(str(UI / "views" / "00_Dashboard.py"), default_timeout=60)
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


def test_there_is_no_pages_directory_for_streamlit_to_discover():
    """The fix that finally held.

    Streamlit auto-discovers a directory named `pages/` and builds a sidebar
    from it. That automatic navigation outlived every attempt to suppress it
    -- gating the pages, gating before st.navigation, registering a hidden
    navigation on the signed-out path -- and a signed-out visitor was shown
    all twenty-two page names plus the entry script.

    st.navigation takes explicit paths, so the directory does not need the
    magic name. Without the name there is nothing to discover and nothing to
    suppress, which is a smaller thing to get right.
    """
    assert not (UI / "pages").exists(), (
        "a directory named pages/ is auto-discovered by Streamlit and will "
        "reappear in the sidebar regardless of st.navigation"
    )
    assert (UI / "views").is_dir()

    body = (UI / "app.py").read_text(encoding="utf-8")
    assert '("pages/' not in body
    # And sign-out is drawn after the navigation, so it sits below the list.
    assert body.index("components.render_sign_out(st)") > body.index(
        "navigation = st.navigation("
    )


def test_the_brand_blue_has_one_value():
    """Streamlit paints the first frame from config.toml, the stylesheet
    paints the rest. Two places holding one colour is how a wordmark ends up
    a different blue from the buttons beside it."""
    import re

    from cfdinvest.ui import theme

    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    declared = re.search(r'primaryColor\s*=\s*"([^"]+)"', config)

    assert declared, "config.toml should declare primaryColor"
    assert declared.group(1).upper() == theme.BRAND["colour"].upper()


def test_the_dashboard_follows_the_viewer_rather_than_forcing_a_mode():
    """Changed deliberately: it used to start dark and offer no choice.

    ``base = "dark"`` with one flat palette meant Streamlit showed "Custom"
    in place of its light/dark control, so the app was dark for everyone and
    nobody could say otherwise. Both modes are branded now and the viewer's
    own preference decides, which is the ordinary behaviour of a web page
    and the reason the switch is back.

    What is given up is the guarantee that a dashboard left open all day is
    not a white rectangle. That is now the viewer's call, which is where it
    belongs.
    """
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")

    assert 'base = "dark"' not in config, (
        "a forced base overrides the per-mode palettes and hides the switch"
    )
    assert "[theme.dark]" in config and "[theme.light]" in config
    assert set(theme.MODES) == {"light", "dark"}, "both modes stay supported"


def test_streamlit_is_pinned_to_the_loopback_in_config():
    """The argument is load-bearing in deployment; without it Streamlit binds
    every interface and answers past the reverse proxy. Set in config too, so
    a local run cannot expose itself either."""
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")

    assert 'address = "127.0.0.1"' in config


def test_a_session_secret_that_cannot_be_saved_says_so(tmp_path, monkeypatch, caplog):
    """The fault that made sign-in fail quietly on the server.

    The dashboard's unit had the auth directory read-only, so the secret
    could not be persisted, was regenerated on every call, and every cookie
    failed to verify. Sign-in appeared to work and then did not stick, with
    nothing in the log connecting the two.
    """
    import logging

    from cfdinvest.ui import auth

    config = auth_config(tmp_path)
    monkeypatch.setattr(auth, "save_credentials", lambda *a, **k: False)

    with caplog.at_level(logging.ERROR):
        secret = auth.session_secret(config)

    assert isinstance(secret, bytes) and len(secret) == 32
    logged = " ".join(record.message for record in caplog.records)
    assert "session secret could not be persisted" in logged
    assert "will" in logged and "not survive" in logged


def test_every_directory_the_dashboard_writes_is_permitted_by_its_unit():
    """The bug that landed twice: sign-in, then recording a trade.

    The unit makes the data root read-only and names its exceptions. Miss
    one and the page fails at the moment you use it -- which is the worst
    possible moment to discover a deployment detail. So the list is derived
    from what the pages actually call rather than kept by hand.
    """
    import re

    unit = (
        Path("deployment/systemd/cfdinvest-dashboard.service")
        .read_text(encoding="utf-8")
    )
    permitted = set(re.findall(r"^ReadWritePaths=(\S+)", unit, re.M))

    # What each writing call lands in, by the store that owns it.
    from cfdinvest.persist import confirm, journal
    from cfdinvest.ui import auth

    # Only what the three remaining pages actually write. Confirm Trades and
    # the Journal are gone, and their write permissions went with them: a
    # path left writable for a page that no longer exists is a permission
    # nothing justifies.
    required = {"auth": auth.AUTH_DIR}
    writers = {"auth": "save_credentials"}
    # auth.py counts: the Account page writes through it rather than
    # calling the store itself.
    pages = " ".join(
        path.read_text(encoding="utf-8") for path in (UI / "views").glob("*.py")
    ) + (UI / "components.py").read_text(encoding="utf-8") + (
        UI / "auth.py"
    ).read_text(encoding="utf-8")

    for label, directory in required.items():
        assert writers[label] in pages, f"nothing writes {label} any more"
        assert f"/var/lib/cfdinvest/{directory}" in permitted, (
            f"the dashboard writes {directory}/ but its unit forbids it"
        )

    # And the root itself stays read-only, which is the point.
    assert "ReadOnlyPaths=/var/lib/cfdinvest" in unit
    assert "/var/lib/cfdinvest" not in permitted, (
        "permitting the root would let a page overwrite a run artefact"
    )
    assert "MemoryMax=" in unit


def test_the_dashboard_does_not_load_the_analysis_frame_to_draw_a_page():
    """What took the deployed dashboard to 3.1GB and got it OOM-killed.

    The analysis artefact is the preprocessed panels in long form: 67MB on
    disk for a 43-instrument run and gigabytes in pandas, because the
    correlation panel is bars x n x n rows. It was read on every page load,
    and no page has ever used it.

    It stays reachable -- as a loader, so a caller pays for it knowingly.
    """
    import inspect

    from cfdinvest.ui import sources

    body = inspect.getsource(sources.load_persisted_run)

    assert "load_analysis_frame" not in body, (
        "loading it eagerly costs gigabytes on every page render"
    )
    # Still reachable, as a plain function. Not a callable inside the bundle:
    # that bundle goes through st.cache_data, which pickles what it stores,
    # and a lambda in there breaks caching for everything else in it.
    assert callable(sources.load_analysis_artefact)
    assert "lambda" not in body


def test_the_sign_in_screen_has_no_sidebar_at_all():
    """Signed out there is nothing to put in it.

    The navigation is hidden and the wordmark is already in the middle of
    the page, so what was left beside the form was an empty 360px panel --
    which reads as a page that failed to load rather than one with nothing
    to show.
    """
    body = (UI / "app.py").read_text(encoding="utf-8")

    gate = body.index("if not components.current_user(st):")
    stop = body.index("st.stop()", gate)

    assert "components.hide_sidebar(st)" in body[gate:stop], (
        "the signed-out path must hide the sidebar before it stops"
    )
    # The wordmark goes in the sidebar's logo slot, so drawing it before the
    # gate would keep an otherwise-empty sidebar alive.
    assert body.index("components.render_brand(st, mode)") > stop, (
        "the brand must not be drawn until someone is signed in"
    )


def test_hiding_the_sidebar_also_hides_the_control_that_reopens_it():
    """An arrow inviting someone to open a panel with nothing behind it."""
    import inspect

    from cfdinvest.ui import components

    css = inspect.getsource(components.hide_sidebar)

    assert '[data-testid="stSidebar"]' in css
    assert "CollapsedControl" in css or "collapsedControl" in css
    assert "display: none" in css


# --------------------------------------------------------------------------- #
# What to do now
# --------------------------------------------------------------------------- #
def signal_row(ticker, direction="long", confidence=0.8, size=0.05, reason=""):
    return {"ticker": ticker, "direction": direction,
            "confidence": confidence, "size": size, "reason": reason}


def test_a_signal_below_the_bar_is_counted_not_shown():
    """You asked not to see trades that are not worth taking.

    A count is still returned, because "nothing clears the bar" and "the
    engine produced nothing" are different states and only one of them
    means something is wrong.
    """
    from cfdinvest.advisor import actions

    kept, below = actions.suggested_trades(
        [signal_row("A", confidence=0.9), signal_row("B", confidence=0.2),
         signal_row("C", confidence=0.59)],
        balance=10_000, threshold=0.6, prices={"A": 100.0},
    )

    assert [s.ticker for s in kept] == ["A"]
    assert below == 2


def test_suggestions_come_back_strongest_first():
    from cfdinvest.advisor import actions

    kept, _ = actions.suggested_trades(
        [signal_row("A", confidence=0.7), signal_row("B", confidence=0.95)],
        balance=10_000, threshold=0.6,
    )
    assert [s.ticker for s in kept] == ["B", "A"]


def test_a_flat_signal_is_never_a_suggestion():
    from cfdinvest.advisor import actions

    kept, below = actions.suggested_trades(
        [signal_row("A", direction="flat", confidence=1.0)],
        balance=10_000, threshold=0.6,
    )
    assert kept == [] and below == 0


def test_the_stake_is_a_share_of_the_balance_not_a_unit_count():
    """The conversion that made a whole simulation a no-op when it was wrong.

    A signal's size is a share of equity. Cash is size x balance, and
    quantity is cash / price -- never size treated as units.
    """
    from cfdinvest.advisor import actions

    kept, _ = actions.suggested_trades(
        [signal_row("A", size=0.05)], balance=10_000, threshold=0.6,
        prices={"A": 250.0},
    )
    suggestion = kept[0]

    assert suggestion.cash == pytest.approx(500.0)
    assert suggestion.quantity == pytest.approx(2.0)
    assert suggestion.actionable


def test_a_suggestion_without_a_price_is_not_actionable():
    """No price means no quantity, and a quantity is the actionable part."""
    from cfdinvest.advisor import actions

    kept, _ = actions.suggested_trades(
        [signal_row("A")], balance=10_000, threshold=0.6, prices={},
    )

    assert kept[0].quantity is None
    assert not kept[0].actionable


def test_a_position_past_its_stop_sorts_above_a_healthy_one():
    """Worst first: the page is read top-down when something is wrong."""
    from cfdinvest.advisor import actions

    watches = actions.positions_to_watch(
        {
            "CALM": {"quantity": 1.0, "price": 100.0},
            "GONE": {"quantity": 1.0, "price": 100.0},
        },
        prices={"CALM": 101.0, "GONE": 89.0},
        stops={"CALM": 90.0, "GONE": 90.0},
    )

    assert [w.ticker for w in watches] == ["GONE", "CALM"]
    assert watches[0].severity == "critical"
    assert watches[1].severity == "pass"


def test_a_position_near_its_stop_is_a_warning():
    from cfdinvest.advisor import actions

    watches = actions.positions_to_watch(
        {"NEAR": {"quantity": 1.0, "price": 100.0}},
        prices={"NEAR": 91.0}, stops={"NEAR": 90.0},
    )

    assert watches[0].severity == "warning"
    assert "stop" in watches[0].note.lower()


def test_a_position_that_cannot_be_priced_is_not_reported_calm():
    """Not knowing what something is worth is not the same as it being fine,
    and this is the page you would read to decide you need do nothing."""
    from cfdinvest.advisor import actions

    watches = actions.positions_to_watch(
        {"DARK": {"quantity": 1.0, "price": 100.0}}, prices={},
    )

    assert watches[0].severity == "unchecked"
    assert watches[0].severity != "pass"
    assert not watches[0].needs_attention


def test_a_closed_position_is_not_watched():
    from cfdinvest.advisor import actions

    assert actions.positions_to_watch({"OUT": {"quantity": 0.0, "price": 1.0}}) == []


def test_the_default_bar_survives_the_percent_round_trip():
    """60% must still mean 0.60 after going through the slider's scale."""
    from cfdinvest.advisor import actions

    percent = round(actions.DEFAULT_CONFIDENCE * 100)
    assert percent == 60
    assert percent / 100.0 == pytest.approx(actions.DEFAULT_CONFIDENCE)


def test_the_poller_prices_what_is_offered_as_well_as_what_is_held():
    """A suggestion carries a stake and a quantity, and both come from a
    price. Priced from yesterday's close, the quantity you are told to place
    is the quantity that was right yesterday."""
    import inspect

    from cfdinvest.livequotes import poller

    body = inspect.getsource(poller.instruments_to_price)

    assert "held_instruments" in body, "open positions are still priced"
    assert "confidence" in body, "and so is anything clearing the bar"

    command = inspect.getsource(
        __import__("cfdinvest.schedule.app", fromlist=["x"])._command_poll_live_quotes
    )
    assert "instruments_to_price" in command


def test_position_costs_count_only_the_position_currently_open(tmp_path):
    """M84: accrued costs summed over the whole run, not since the open.

    The positions page charged one open position with every cost the run
    had ever booked on that instrument. On the deployed book that showed
    £326 of accrued financing against £2.55 of real accrual on a position a
    few days old -- a 128x overstatement that read as a plausible number,
    which is exactly why it survived being looked at.
    """
    import pandas as pd

    # Two Apple positions in the run's history: one long closed, one open.
    trades = pd.DataFrame(
        [
            {"asset": "AAPL", "timestamp": pd.Timestamp("2024-01-02", tz="UTC"),
             "action": "open_long", "financing": 100.0, "spread_cost": 1.0},
            {"asset": "AAPL", "timestamp": pd.Timestamp("2024-02-01", tz="UTC"),
             "action": "close", "financing": 200.0, "spread_cost": 1.0},
            {"asset": "AAPL", "timestamp": pd.Timestamp("2024-06-03", tz="UTC"),
             "action": "open_short", "financing": 3.0, "spread_cost": 0.5},
            {"asset": "AAPL", "timestamp": pd.Timestamp("2024-06-05", tz="UTC"),
             "action": "resize", "financing": 4.0, "spread_cost": 0.5},
        ]
    )

    COST_COLUMNS = ("financing", "spread_cost")

    def since_open(frame):
        opens = frame.index[frame["action"].str.startswith("open")]
        if len(opens):
            frame = frame.loc[opens[-1]:]
        return frame[[c for c in COST_COLUMNS if c in frame.columns]].sum()

    accrued = (
        trades.sort_values("timestamp").reset_index(drop=True)
        .groupby("asset", group_keys=False)
        .apply(since_open, include_groups=False)
    )

    # Only the 3.0 + 4.0 booked since the still-open short was taken.
    assert float(accrued.loc["AAPL", "financing"]) == pytest.approx(7.0)
    assert float(accrued.loc["AAPL", "spread_cost"]) == pytest.approx(1.0)
    # The whole history would have been 307.0, which is the bug.
    assert float(trades["financing"].sum()) == pytest.approx(307.0)


# --------------------------------------------------------------------------- #
# The dashboard reports the paper book
# --------------------------------------------------------------------------- #
def test_the_dashboard_never_pools_the_arms_into_a_verdict(populated):
    """M141: three thresholds averaged into one number.

    The arms exist to be compared. A single pooled win rate would answer
    the question they were built to ask by erasing it -- and it would drift
    with whichever arm happened to trade most, not with which threshold was
    right. The headline may pool counts; the comparison must stay per arm.
    """
    page = (UI / "views" / "00_Dashboard.py").read_text(encoding="utf-8")

    assert "cohorts" in page
    # Each arm rendered on its own, not summed into a verdict.
    assert "for cell, arm in zip" in page or "for arm in cohorts" in page
    assert "0.50" in page and "0.60" in page and "0.70" in page


def test_the_dashboard_withholds_a_win_rate_below_the_floor(populated):
    """A rate on a handful of trades is a number that gets quoted.

    The figures stay available -- `rate_so_far` is shown, greyed, with how
    far there is to go -- but the claim is withheld until the sample
    supports it. That is the same rule the forward record runs on, and the
    reason for it is that a -5.53% which looked like a finding on fifteen
    ticker-months inverted to +2.37% on four hundred and fifty-six.
    """
    from cfdinvest.advisor import papertrade

    book = []
    for index in range(3):
        book.append({"event": "open", "cohort": "c50", "ticker": f"T{index}",
                     "at": f"2026-08-0{index + 1}"})
        book.append({"event": "close", "cohort": "c50", "ticker": f"T{index}",
                     "at": f"2026-08-0{index + 2}", "net_pnl_gbp": 5.0})

    summary = papertrade.summarise(book, "c50")
    assert summary["closed"] == 3
    assert summary["win_rate"] is None, "a rate on three trades is not a claim"
    assert summary["rate_so_far"] == pytest.approx(100.0)
    assert summary["needed"] == papertrade.MIN_CLOSED_FOR_RATE - 3

    # And above the floor it becomes one.
    book = []
    for index in range(papertrade.MIN_CLOSED_FOR_RATE):
        book.append({"event": "open", "cohort": "c50", "ticker": f"T{index}",
                     "at": "2026-08-01"})
        book.append({"event": "close", "cohort": "c50", "ticker": f"T{index}",
                     "at": "2026-08-02", "net_pnl_gbp": 5.0})
    assert papertrade.summarise(book, "c50")["win_rate"] == pytest.approx(100.0)


def test_the_paper_book_components_sum_to_the_total():
    """The accounting identity, asserted rather than assumed.

    Gross less financing, spread and the conversion fee equals net. The
    dashboard shows all five side by side, so if they ever disagreed the
    page would be presenting two numbers that do not reconcile and calling
    both facts.
    """
    from cfdinvest.advisor import papertrade

    book = [
        {"event": "open", "cohort": "c60", "ticker": "AAA", "at": "2026-08-01"},
        {"event": "close", "cohort": "c60", "ticker": "AAA", "at": "2026-08-02",
         "gross_pnl_gbp": 18.0, "financing_gbp": 5.0, "spread_cost_gbp": 0.4,
         "fx_fee_gbp": 0.6, "net_pnl_gbp": 12.0, "days_held": 1.0},
    ]
    s = papertrade.summarise(book, "c60")
    residual = (
        s["gross_gbp"] - s["financing_gbp"] - s["spread_gbp"] - s["fx_fee_gbp"]
    )
    assert residual == pytest.approx(s["net_gbp"])


def test_an_unpriced_close_is_excluded_rather_than_counted_as_zero():
    """Absence is named. A trade that could not be valued is not a scratch."""
    from cfdinvest.advisor import papertrade

    book = [
        {"event": "open", "cohort": "c50", "ticker": "AAA", "at": "2026-08-01"},
        {"event": "close", "cohort": "c50", "ticker": "AAA", "at": "2026-08-02",
         "net_pnl_gbp": None},
        {"event": "open", "cohort": "c50", "ticker": "BBB", "at": "2026-08-01"},
        {"event": "close", "cohort": "c50", "ticker": "BBB", "at": "2026-08-02",
         "net_pnl_gbp": 4.0},
    ]
    s = papertrade.summarise(book, "c50")
    assert s["closed"] == 1, "the unpriced trade must not be counted"
    assert s["unpriced"] == 1, "and it must not be silently dropped either"
    assert s["net_gbp"] == pytest.approx(4.0)
    assert s["losses"] == 0, "unpriced is not a loss"


def test_a_frame_too_large_to_style_is_trimmed_rather_than_fatal():
    """M143: a table that grows until it takes the page down.

    The signal history reached 295,734 cells against a Styler ceiling of
    262,144, and Streamlit does not degrade at that ceiling -- it raises,
    inside st.dataframe, which took out the whole Signals page rather than
    the one table. The history grows with every pipeline run, so any fixed
    ceiling is reached eventually and raising the limit only postpones it.

    Trimmed, and the trim is stated. A table silently showing 40% of the
    rows reads as the whole of them, which is a worse failure than the
    crash it replaces.
    """
    import numpy as np

    from cfdinvest.ui import components as ui

    columns = 6
    rows = ui.STYLER_CELL_LIMIT // columns + 5_000
    frame = pd.DataFrame(
        np.zeros((rows, columns)), columns=[f"c{i}" for i in range(columns)]
    )
    assert frame.size > ui.STYLER_CELL_LIMIT, "the fixture must exceed the ceiling"

    drawn: dict = {"frames": [], "notes": []}

    class _Stub:
        def dataframe(self, data, **kwargs):
            # Styler or frame -- either way, count what it would render.
            underlying = getattr(data, "data", data)
            drawn["frames"].append(underlying)

        def markdown(self, body, **kwargs):
            drawn["notes"].append(str(body))

        caption = markdown
        info = markdown

    ui.render_frame(_Stub(), frame)

    assert drawn["frames"], "nothing was drawn at all"
    rendered = drawn["frames"][-1]
    assert rendered.size <= ui.STYLER_CELL_LIMIT, "still too large to style"
    assert len(rendered) < rows, "nothing was trimmed"

    # And it must say so.
    said = " ".join(drawn["notes"])
    assert f"{rows:,}" in said, "the true row count is not stated"
    assert f"{len(rendered):,}" in said, "the shown row count is not stated"


def test_a_frame_that_fits_is_not_trimmed_or_annotated():
    """The guard must not narrate on every ordinary table."""
    from cfdinvest.ui import components as ui

    frame = pd.DataFrame({"a": range(50), "b": range(50)})
    drawn: dict = {"frames": [], "notes": []}

    class _Stub:
        def dataframe(self, data, **kwargs):
            drawn["frames"].append(getattr(data, "data", data))

        def markdown(self, body, **kwargs):
            drawn["notes"].append(str(body))

        caption = markdown
        info = markdown

    ui.render_frame(_Stub(), frame)
    assert len(drawn["frames"][-1]) == 50
    assert not any("Showing the first" in note for note in drawn["notes"])


def test_the_status_table_lists_every_job_the_daemon_runs():
    """M144: a job running with nobody watching it.

    Two statements of one fact. paper-book and advisor-alert ran on the box
    every five minutes and appeared on no status page, so the forward record
    looked stopped while it was working -- and the only way to find out
    otherwise was to read the journal over SSH.

    Asserted both ways: a job the daemon runs and the table omits is
    unwatched, and a row for a job the daemon does not run is a promise the
    system never keeps.
    """
    from cfdinvest.schedule import daemon
    from cfdinvest.ui import vitals

    listed = {name for name, _, _ in vitals.SCHEDULES}
    assert listed == set(daemon.JOBS), (
        f"unwatched: {sorted(set(daemon.JOBS) - listed)}; "
        f"promised but never run: {sorted(listed - set(daemon.JOBS))}"
    )


def test_a_restart_does_not_make_every_job_due_at_once(tmp_path, monkeypatch):
    """M145: the daemon forgetting what it ran.

    ``last_run`` lived in a local variable, so a restart presented an empty
    map to ``jobs_due`` and every daily job came due immediately. With a
    deploy restarting the daemon, the pipeline fired on every push -- 62 runs
    in two days, each writing a 69 MB analysis frame over a lake that had
    not changed.
    """
    from cfdinvest.persist import scheduler_state
    from cfdinvest.schedule import daemon

    # Both seams, because reads and writes go through different names --
    # load_scheduler_state resolves the path one way and write_scheduler_state
    # another. Patching only the read seam sent writes to the real store and
    # the test then passed on its own leftovers from an earlier run.
    state_file = tmp_path / "state.json"
    for name in ("get_scheduler_state_path", "ensure_scheduler_state"):
        assert hasattr(scheduler_state, name), f"{name} is not the seam any more"
        monkeypatch.setattr(
            scheduler_state, name, lambda config=None, account=None: state_file
        )

    now = pd.Timestamp("2026-08-26T14:00:00Z")
    # Nothing remembered: the daily jobs are due, which is right on a cold
    # start and wrong on a restart twenty minutes after they ran.
    assert "run-daily" in daemon.jobs_due(now, {})

    scheduler_state.record_job("run-daily", pd.Timestamp("2026-08-26T03:05:00Z"))
    scheduler_state.record_job("ingest-daily", pd.Timestamp("2026-08-26T03:00:00Z"))

    remembered = scheduler_state.last_job_runs()
    assert "run-daily" in remembered, "the job was not persisted"

    # Seeded from disk, today's run is not owed again.
    assert "run-daily" not in daemon.jobs_due(now, remembered)
    assert "ingest-daily" not in daemon.jobs_due(now, remembered)

    # And tomorrow it is owed once more -- this must not become a job that
    # never runs again.
    tomorrow = pd.Timestamp("2026-08-27T14:00:00Z")
    assert "run-daily" in daemon.jobs_due(tomorrow, remembered)


def test_signals_are_refreshed_during_the_session_not_once_a_day():
    """M149: a trading cadence that cannot see intraday moves.

    The pipeline ran once at 03:05 UTC and the paper book then re-read the
    same daily bar every five minutes. Four consecutive runs across forty
    minutes produced the same two signals with confidence moving by 0.002,
    because re-reading an unchanged bar cannot produce a different answer --
    a CFD book was being offered one decision per instrument per session.
    """
    from cfdinvest.schedule import daemon

    ran_at_three = {
        "ingest-daily": "2026-08-26T03:00:00Z",
        "run-daily": "2026-08-26T03:05:00Z",
        "poll-live-quotes": "2026-08-26T14:59:00Z",
    }
    midday = "2026-08-26T15:00:00Z"

    due = daemon.jobs_due(midday, ran_at_three, market_open=True)
    assert "run-intraday" in due, "no fresh signals during the session"
    # Bars before signals, or the pipeline reads a lake still being written.
    assert due.index("ingest-intraday") < due.index("run-intraday")

    # Not again a minute later.
    just_ran = {**ran_at_three, "run-intraday": "2026-08-26T14:58:00Z"}
    assert "run-intraday" not in daemon.jobs_due(midday, just_ran, market_open=True)

    # And again once the interval has passed.
    later = "2026-08-26T15:20:00Z"
    assert "run-intraday" in daemon.jobs_due(later, just_ran, market_open=True)


def test_nothing_intraday_runs_while_the_market_is_shut():
    """Trading hours are a constraint, not a preference.

    A fill at a price nobody was quoting is not a fill. An unknown calendar
    counts as shut for the same reason: pausing costs a few bars, trading on
    a guess costs whatever the guess was worth.
    """
    from cfdinvest.schedule import daemon

    history = {
        "ingest-daily": "2026-08-26T03:00:00Z",
        "run-daily": "2026-08-26T03:05:00Z",
        "poll-live-quotes": "2026-08-26T02:00:00Z",
    }
    for state in (False, None):
        due = daemon.jobs_due("2026-08-26T23:00:00Z", history, market_open=state)
        assert "run-intraday" not in due, f"traded with market_open={state}"
        assert "ingest-intraday" not in due


def test_the_snapshots_stay_on_the_daily_run():
    """M150: ninety operational records a session, all saying the same thing.

    The snapshots and the prune describe a day. Tied to the intraday run
    they would fire every fifteen minutes, and a prune that runs before the
    run it would prune is worse than useless.
    """
    from cfdinvest.schedule import daemon

    intraday_only = daemon.jobs_due(
        "2026-08-26T15:00:00Z",
        {"ingest-daily": "2026-08-26T03:00:00Z",
         "run-daily": "2026-08-26T03:05:00Z",
         "poll-live-quotes": "2026-08-26T14:59:00Z"},
        market_open=True,
    )
    assert "run-intraday" in intraday_only
    for job in ("monitoring-snapshot", "risk-snapshot", "prune-runs"):
        assert job not in intraday_only, f"{job} fired on the intraday cadence"

    # They still follow the daily run.
    daily = daemon.jobs_due("2026-08-27T03:10:00Z", {}, market_open=False)
    assert "run-daily" in daily
    for job in ("monitoring-snapshot", "risk-snapshot", "prune-runs"):
        assert job in daily


@pytest.fixture
def dashboard_with_a_position(monkeypatch, populated):
    """A paper book holding one open position, so the marking path runs.

    Patched at ``sources.load_paper_book`` rather than at the storage path.
    The first version of this patched ``papertrade._path``, which the cached
    bundle never consulted, so the page found no book, stopped early, and
    the whole open-positions block went unexercised -- the fixture looked
    like coverage and was not. A missing import inside that block survived
    the entire suite because of it.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import load_rules
    from cfdinvest.ui import sources

    trade = papertrade.open_trade(
        "AAPL", "long", 200.0, "2026-08-26T14:00:00Z",
        rules=load_rules("config/t212_rules.toml"), fx_rate=0.7355,
        cohort="c50", state={"confidence": 0.72, "regime": "risk_on"},
    )
    book = [trade.as_row()]

    monkeypatch.setattr(
        sources, "load_paper_book",
        lambda config, account=None: sources.Source(
            value={
                "book": book,
                "cohorts": papertrade.summarise_all(book),
                "pooled": papertrade.summarise(book),
                "min_closed": papertrade.MIN_CLOSED_FOR_RATE,
                "stake": papertrade.STAKE_GBP,
            }
        ),
    )
    return book


def test_the_dashboard_marks_open_positions_against_the_latest_quote(
    dashboard_with_a_position, monkeypatch
):
    """M156: the open-position block never exercised.

    It was not: the fixture book was always empty, so the page stopped
    before the block and a NameError sitting in it survived the whole
    suite. It would have fired on the live dashboard the moment the first
    trade opened, which is the worst possible time to find an import.
    """
    from cfdinvest.ui import sources

    monkeypatch.setattr(
        sources, "load_live_quotes",
        lambda config, account=None: sources.Source(
            value={
                "quotes": {"AAPL": 204.0},
                "taken_at": "2026-08-26T17:00:00Z",
                "age_minutes": 3.0,
                "limit_minutes": 15.0,
                "stale": False,
                "unavailable": [],
            }
        ),
    )

    result = render("views/00_Dashboard.py")
    assert not result.exception, f"dashboard raised: {result.exception}"

    drawn = " ".join(str(e.value) for e in result.markdown)
    drawn += " ".join(str(e.value) for e in result.caption)
    assert "Open positions" in drawn or any(
        "Open positions" in str(e.value) for e in result.subheader
    ), "the page stopped before the open-position block -- fixture is not covering it"

    # M157: the age must be stated, not merely the value.
    #
    # Asserted on the marks caption specifically. A looser search for
    # "minutes ago" anywhere on the page passed against a mutation that
    # removed it entirely, because the sidebar's freshness line says the
    # same words about something else.
    captions = [str(e.value) for e in result.caption]
    dated = [c for c in captions if "Marked at" in c]
    assert dated, "the marks carry no timestamp of their own"
    assert any("minutes ago" in c for c in dated), (
        "the marks are timestamped but not aged -- how far behind the "
        "market they are is the whole point"
    )


def test_a_position_that_cannot_be_priced_is_still_shown(
    dashboard_with_a_position, monkeypatch
):
    """Absence is named. A position you cannot value is the one to look at."""
    from cfdinvest.advisor import papertrade
    from cfdinvest.ui import sources

    monkeypatch.setattr(
        sources, "load_live_quotes",
        lambda config, account=None: sources.Source.missing(
            "no live quote has been polled yet", value={}
        ),
    )
    result = render("views/00_Dashboard.py")
    assert not result.exception, f"dashboard raised: {result.exception}"

    drawn = " ".join(str(e.value) for e in result.markdown)
    assert "not live" in drawn, "an unknown quote age was not called out"

    marks = papertrade.mark_open_positions(
        dashboard_with_a_position, {}, "2026-08-26T17:00:00Z", "c50"
    )
    assert len(marks) == 1, "an unpriced position was dropped from the table"
    assert marks[0]["priced"] is False
    assert marks[0]["value_gbp"] is None, "an unvalued position must not show a value"


def test_the_daily_run_is_not_dated_by_the_intraday_one():
    """M161: two false warnings for a scheduler doing as it was told.

    run-daily and run-intraday both write a run directory, so the newest
    directory cannot say which of them made it. Dating run-daily by it put
    the intraday run's time under the daily row, and then marked the
    snapshots that correctly follow the daily run as "older than the most
    recent run" -- risk-snapshot and prune-runs both reported behind
    schedule while the daemon's own record showed them running seconds
    after run-daily, exactly as designed.

    A status table that cries wolf is worse than no status table.
    """
    import pandas as pd

    from cfdinvest.ui import vitals

    # The daily run and its snapshots at 03:05; an intraday run since.
    evidence = {
        "poll-live-quotes": "2026-08-26T17:20:00Z",
        "advisor-alert": "2026-08-26T17:20:00Z",
        "paper-book": "2026-08-26T17:20:00Z",
        "ingest-daily": "2026-08-26T03:00:00Z",
        "run-daily": "2026-08-26T03:05:00Z",
        "ingest-intraday": "2026-08-26T17:10:00Z",
        "run-intraday": "2026-08-26T17:10:00Z",
        "monitoring-snapshot": "2026-08-26T03:05:30Z",
        "risk-snapshot": "2026-08-26T03:05:31Z",
        "prune-runs": "2026-08-26T03:05:32Z",
    }
    rows = {
        row.name: row
        for row in vitals.job_report(
            evidence, now=pd.Timestamp("2026-08-26T17:25:00Z"), session_open=True
        )
    }

    for job in ("risk-snapshot", "prune-runs", "monitoring-snapshot"):
        assert rows[job].status == "pass", (
            f"{job} reported {rows[job].status}: {rows[job].detail}"
        )

    # And a snapshot that genuinely lags its own daily run is still caught.
    stale = dict(evidence, risk_snapshot=None)
    stale["risk-snapshot"] = "2026-08-25T03:05:31Z"
    stale["run-daily"] = "2026-08-26T03:05:00Z"
    lagging = {
        row.name: row
        for row in vitals.job_report(
            stale, now=pd.Timestamp("2026-08-26T17:25:00Z"), session_open=True
        )
    }
    assert lagging["risk-snapshot"].status == "warning", (
        "a snapshot older than its own daily run must still be flagged"
    )


def test_the_daily_run_row_is_dated_by_the_daemon_not_the_run_directory(monkeypatch):
    """The seam the previous test missed.

    Asserting on job_report with hand-fed evidence proved the *reporting*
    logic was sound while leaving the actual defect untouched -- it lived in
    gather_evidence, which chose the wrong source. The first version of this
    test passed happily against the mutation that reintroduced it.
    """
    import pandas as pd

    from cfdinvest.persist import readers, scheduler_state
    from cfdinvest.ui import vitals

    # A run directory stamped by the most recent *intraday* run.
    monkeypatch.setattr(
        readers, "load_latest_run", lambda *a, **k: "20260826T171000Z"
    )
    monkeypatch.setattr(
        scheduler_state, "last_job_runs",
        lambda *a, **k: {
            "run-daily": "2026-08-26T03:05:00Z",
            "run-intraday": "2026-08-26T17:10:00Z",
        },
    )

    evidence = vitals.gather_evidence(None)

    daily = pd.Timestamp(evidence["run-daily"])
    assert daily.hour == 3, (
        f"run-daily dated {daily} -- that is the intraday run's directory, "
        "not the daily job"
    )
    assert pd.Timestamp(evidence["run-intraday"]).hour == 17


def test_the_signals_page_leads_with_what_is_actionable(populated):
    """M162: fifteen equal cards and no answer to "should I do anything".

    The page opened with one bordered card per instrument, two hundred
    pixels tall, in ticker order -- so the single signal clearing the bar
    was as prominent as the fourteen that did not, and whether anything was
    actionable required scrolling the lot.
    """
    page = (UI / "views" / "02_Signals.py").read_text(encoding="utf-8")

    # A summary before the list.
    assert "clear the" in page and "bar" in page
    assert page.index("headline") < page.index('st.subheader("Current signals")')

    result = render("views/02_Signals.py")
    assert not result.exception, f"signals page raised: {result.exception}"


def test_signals_are_ordered_by_confidence_not_by_ticker(populated):
    """Highest conviction first.

    Ticker order treats a 90% signal and a 9% one as equally worth reading,
    which is the opposite of true.
    """
    page = (UI / "views" / "02_Signals.py").read_text(encoding="utf-8")

    assert 'sort_values("_confidence", ascending=False' in page, (
        "the signal list is not ordered by confidence"
    )
    # And not by ticker, which is what it did before.
    assert "sort_values(TICKER" not in page


def test_the_signal_list_stays_compact(populated):
    """M163: the tall cards coming back.

    Each signal is one row of columns. A bordered container per instrument
    is what made the page unreadable at fifteen signals, and the universe
    has twenty-two.
    """
    page = (UI / "views" / "02_Signals.py").read_text(encoding="utf-8")

    body = page[page.index('st.subheader("Current signals")'):]
    body = body[:body.index("st.divider()")]
    assert "container(border=True)" not in body, (
        "a bordered card per signal is back"
    )
    assert "#### " not in body, "a heading per signal is back"


def test_the_streamlit_theme_brands_both_modes():
    """M164: branding the app at the cost of the reader's light/dark switch.

    A single flat ``[theme]`` palette replaces Streamlit's light/dark
    control with "Custom" and nothing else, so specifying brand colours
    silently removed any way to choose. The sub-theme form brands both and
    leaves the switch working.

    The values are asserted against theme.PALETTES rather than restated, so
    the config and the stylesheet cannot drift into being two different
    blues.
    """
    import tomllib

    from cfdinvest.ui import theme

    config = tomllib.loads(
        (Path(".streamlit") / "config.toml").read_text(encoding="utf-8")
    )
    block = config.get("theme") or {}

    assert "light" in block and "dark" in block, (
        "only one mode is branded -- Streamlit will drop the light/dark switch"
    )
    # A flat palette alongside the sub-themes would reintroduce the problem.
    for flat in ("backgroundColor", "secondaryBackgroundColor", "textColor"):
        assert flat not in block, f"theme.{flat} is set flat, overriding both modes"

    for mode in ("light", "dark"):
        palette = theme.PALETTES[mode]
        assert block[mode]["backgroundColor"].upper() == palette["background"].upper()
        assert (
            block[mode]["secondaryBackgroundColor"].upper() == palette["panel"].upper()
        )
        assert block[mode]["textColor"].upper() == palette["text"].upper()

    assert block["primaryColor"].upper() == theme.BRAND["colour"].upper(), (
        "the wordmark blue and the widget blue have drifted apart"
    )



def test_the_sidebar_draws_no_rule_of_its_own_under_the_navigation():
    """M165: two horizontal rules with a gap between them.

    st.navigation already separates the page list from whatever follows.
    Adding another put two lines at the top of every sidebar, which reads
    as a section that failed to render rather than as a divider.
    """
    import inspect

    from cfdinvest.ui import components as ui

    source = inspect.getsource(ui.render_sign_out)
    assert "st.divider()" not in source, (
        "render_sign_out draws a rule Streamlit has already drawn"
    )


def test_the_status_page_does_not_repeat_the_wordmark_above_its_title():
    """The crumb said "CFDINVEST Status" directly above a heading reading
    "System status", and was clipped by the toolbar besides. The wordmark is
    in the sidebar; a page does not need to introduce itself twice."""
    page = (UI / "views" / "01_Status.py").read_text(encoding="utf-8")
    assert "render_crumb" not in page


def test_signing_out_survives_a_cookie_that_is_still_in_the_browser():
    """M170: the Log out button that did nothing.

    Clearing the cookie and rerunning cannot happen in one script run. The
    cookie is written by a zero-height component, and a component only
    reaches the browser when the run it belongs to finishes rendering --
    ``st.rerun()`` aborts that run, so the clearing cookie was never sent.
    The cookie stayed valid, ``current_user`` read it on the very next run
    and signed the user straight back in, and the button appeared inert.
    Refreshing did not help, because refreshing is what re-read the cookie.

    So the flag has to outrank the cookie for exactly one run.
    """
    from cfdinvest.ui import auth as auth_module
    from cfdinvest.ui import components as ui

    class Context:
        cookies: dict = {}
        headers: dict = {}

    class Fake:
        def __init__(self):
            self.session_state = {}
            self.context = Context()

    st = Fake()
    Context.cookies = {auth_module.COOKIE_NAME: "a-valid-looking-token"}
    st.session_state[auth_module.SESSION_KEY] = "admin"
    assert ui.current_user(st) == "admin"

    ui.sign_out(st)

    # The cookie is still there -- nothing has reached the browser yet.
    assert Context.cookies, "the fixture must keep the cookie to be meaningful"
    assert ui.current_user(st) is None, (
        "the browser cookie signed the user back in before it could be cleared"
    )
    assert st.session_state.get(ui.SIGNED_OUT_KEY) is True

    # Signing back in must not be haunted by the flag.
    st.session_state.pop(ui.SIGNED_OUT_KEY, None)
    st.session_state[auth_module.SESSION_KEY] = "admin"
    assert ui.current_user(st) == "admin"


def test_the_cookie_is_cleared_on_a_run_that_finishes():
    """The other half: sign_out must not try to write it itself.

    Writing it there is what failed, because the caller reruns immediately
    afterwards. require_login does it instead, on a pass that renders the
    form and completes.
    """
    import inspect

    from cfdinvest.ui import components as ui

    out = inspect.getsource(ui.sign_out)
    assert "_write_cookie" not in out, (
        "sign_out writes the cookie again -- the rerun will discard it"
    )

    login = inspect.getsource(ui.require_login)
    assert "SIGNED_OUT_KEY" in login and "_write_cookie" in login, (
        "nothing clears the cookie on a run that finishes"
    )
