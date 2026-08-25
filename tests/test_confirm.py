"""Confirmations: what actually happened, against what was recommended.

The layer exists to replace an assumption with a fact, so most of what
follows is about the ways that replacement could go wrong quietly — history
rewritten, an unconfirmed run read as a refused one, a quoted recommendation
in a reply chain read as a confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.confirm import reply as reply_module
from cfdinvest.confirm.recommendation import account_scale, recommendation_for
from cfdinvest.persist import confirm as store
from cfdinvest.persist.confirm import (
    Confirmation,
    confirmed_positions,
    deviations,
    load_confirmations,
    load_latest_confirmation,
    merge_simulated_and_confirmed_positions,
    write_confirmation,
)

NOW = pd.Timestamp("2026-08-25 09:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def opened(ticker="SPX500", **changes) -> Confirmation:
    arguments = dict(
        direction="long", quantity=0.684, price=4434.8, timestamp=NOW
    )
    arguments.update(changes)
    return Confirmation.of(ticker, "open", **arguments)


def panel(**held) -> pd.DataFrame:
    index = pd.bdate_range("2026-08-20", periods=3, tz="UTC", name="timestamp")
    return pd.DataFrame(
        {name: [0.0, value / 2, value] for name, value in held.items()}, index=index
    )


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
def test_direction_sets_the_sign_once():
    """positions.parquet holds signed units, so a confirmation must too."""
    assert opened(direction="long").quantity == pytest.approx(0.684)
    assert opened(direction="short").quantity == pytest.approx(-0.684)


def test_a_signed_quantity_survives_without_a_direction():
    assert Confirmation("X", "open", price=100.0, quantity=-2.0).direction == "short"


def test_exposure_is_computed_when_it_is_not_given():
    assert opened().exposure == pytest.approx(0.684 * 4434.8)


def test_an_explicit_exposure_is_kept():
    """The ticket shows a figure; re-deriving it invites a disagreement."""
    assert opened(exposure=3_000.0).exposure == pytest.approx(3_000.0)


def test_a_close_needs_no_quantity():
    assert Confirmation("X", "close", price=100.0).quantity == 0.0


def test_an_ignore_needs_no_price():
    """Recording that you passed is not recording a fill."""
    assert Confirmation("X", "ignore", notes="spread too wide").price is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(price=0.0), "price must be > 0"),
        (dict(price=-1.0), "price must be > 0"),
        (dict(quantity=0.0), "non-zero quantity"),
    ],
)
def test_a_bad_open_is_refused_at_the_door(kwargs, message):
    """A wrong fill price does not look wrong downstream — it looks certain."""
    with pytest.raises(ValueError, match=message):
        opened(**kwargs)


def test_an_open_with_no_price_is_refused():
    with pytest.raises(ValueError, match="needs a fill price"):
        Confirmation("X", "open", quantity=1.0)


def test_a_flip_needs_both_legs():
    """Two fills. Averaging them would lose both."""
    with pytest.raises(ValueError, match="close_price"):
        Confirmation.of("X", "flip", direction="short", quantity=1.0, price=100.0)


def test_an_unknown_action_is_refused():
    with pytest.raises(ValueError, match="action must be one of"):
        Confirmation("X", "hedge", price=1.0, quantity=1.0)


def test_a_ticker_that_is_not_a_ticker_is_refused():
    """The name becomes a filename, and later a column."""
    with pytest.raises(ValueError, match="plain instrument code"):
        Confirmation("../../etc/passwd", "close", price=1.0)


def test_a_naive_timestamp_becomes_utc():
    assert Confirmation(
        "X", "close", price=1.0, timestamp="2026-08-25 09:00"
    ).timestamp == NOW


def test_slippage_reads_the_same_way_for_both_directions():
    """Positive is worse than recommended, long or short."""
    long_paid_more = opened(price=4440.0, recommended_price=4434.8)
    short_sold_lower = opened(
        direction="short", price=4429.6, recommended_price=4434.8
    )
    assert long_paid_more.slippage == pytest.approx(5.2)
    assert short_sold_lower.slippage == pytest.approx(5.2)


def test_slippage_is_absent_without_a_recommendation():
    assert opened().slippage is None


def test_a_partial_fill_shows_as_a_fraction():
    assert opened(quantity=0.5, recommended_quantity=1.0).quantity_deviation == 0.5


def test_a_deviation_against_nothing_is_absent():
    """A trade the system never suggested is not a deviation from anything."""
    assert opened(recommended_quantity=0.0).quantity_deviation is None


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_a_confirmation_round_trips(lake):
    path = write_confirmation(opened(), lake)

    assert Path(path).name == "20260825-090000-OPEN-SPX500.json"

    found = load_confirmations(lake)
    assert len(found) == 1
    assert found[0].price == pytest.approx(4434.8)
    assert found[0].direction == "long"


def test_the_filename_says_what_it_is_without_being_opened(lake):
    write_confirmation(Confirmation("NAS100", "close", price=1.0, timestamp=NOW), lake)
    assert Path(store.list_confirmations(lake)[0]).name == (
        "20260825-090000-CLOSE-NAS100.json"
    )


def test_two_in_the_same_second_do_not_overwrite(lake):
    """Append-only means the second must not land on the first."""
    write_confirmation(opened(), lake)
    write_confirmation(opened(price=4435.9), lake)

    assert len(store.list_confirmations(lake)) == 2
    assert {c.price for c in load_confirmations(lake)} == {4434.8, 4435.9}


def test_the_write_is_atomic(lake):
    """No half-written JSON, ever: a torn confirmation reads as a fact."""
    write_confirmation(opened(), lake)
    for path in store.list_confirmations(lake):
        json.loads(path.read_text(encoding="utf-8"))
    assert not list(store.confirmations_root(lake).glob("*.tmp"))


def test_a_failed_write_raises_rather_than_returning_none(lake, monkeypatch):
    """Unlike the rest of the persistence layer, and deliberately.

    A snapshot that failed to record is a lost reading. A confirmation that
    failed to record leaves the system believing the recommendation was
    followed, which is worse than an error.
    """
    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        write_confirmation(opened(), lake)


def test_a_corrupt_file_does_not_hide_the_others(lake):
    write_confirmation(opened(), lake)
    (store.confirmations_root(lake) / "20260825-100000-OPEN-X.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert len(load_confirmations(lake)) == 1


def test_the_latest_for_one_instrument(lake):
    write_confirmation(opened(price=4434.8), lake)
    write_confirmation(opened(price=4440.0, timestamp=NOW + pd.Timedelta("1h")), lake)
    write_confirmation(opened("NAS100", price=18_923.4), lake)

    assert load_latest_confirmation("SPX500", lake).price == pytest.approx(4440.0)
    assert load_latest_confirmation("GLD", lake) is None


def test_no_confirmations_is_an_empty_list(lake):
    assert load_confirmations(lake) == []


def test_a_document_from_a_later_version_still_loads(lake):
    """An unknown field must not make an old client refuse a good record."""
    document = opened().to_document() | {"invented_later": True}
    (store.confirmations_root(lake)).mkdir(parents=True, exist_ok=True)
    (store.confirmations_root(lake) / "20260825-090000-OPEN-SPX500.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    assert load_confirmations(lake)[0].price == pytest.approx(4434.8)


# --------------------------------------------------------------------------- #
# Replaying into a book
# --------------------------------------------------------------------------- #
def test_a_confirmation_states_the_position_not_a_delta():
    """It is what the ticket shows you, and what you can check by looking."""
    book = confirmed_positions([opened(quantity=1.0), opened(quantity=3.0,
                                timestamp=NOW + pd.Timedelta("1h"))])
    assert book["SPX500"]["quantity"] == pytest.approx(3.0)


def test_a_close_takes_the_position_to_zero():
    book = confirmed_positions(
        [
            opened(),
            Confirmation("SPX500", "close", price=4450.0,
                         timestamp=NOW + pd.Timedelta("1h")),
        ]
    )
    assert book["SPX500"]["quantity"] == 0.0


def test_ignoring_records_a_decision_not_a_position():
    book = confirmed_positions(
        [Confirmation("SPX500", "ignore", timestamp=NOW)]
    )
    assert book == {}


def test_replay_is_in_time_order_not_file_order():
    later = opened(quantity=2.0, timestamp=NOW + pd.Timedelta("2h"))
    earlier = opened(quantity=1.0, timestamp=NOW)
    assert confirmed_positions([later, earlier])["SPX500"]["quantity"] == 2.0


# --------------------------------------------------------------------------- #
# The merge
# --------------------------------------------------------------------------- #
def test_a_confirmation_overrides_the_simulated_position():
    merged, report = merge_simulated_and_confirmed_positions(
        panel(SPX500=1.0), [opened(quantity=0.684)]
    )
    assert merged.iloc[-1]["SPX500"] == pytest.approx(0.684)
    assert report["overridden"]["SPX500"] == {"simulated": 1.0, "confirmed": 0.684}


def test_history_is_never_rewritten():
    """A confirmation says what you hold now, not what you held in June."""
    simulated = panel(SPX500=1.0)
    merged, _ = merge_simulated_and_confirmed_positions(
        simulated, [opened(quantity=0.684)]
    )
    assert merged.iloc[:-1].equals(simulated.iloc[:-1])


def test_an_unconfirmed_instrument_keeps_its_simulated_position():
    """Silence is not a decision; the engine's belief stands until contradicted."""
    merged, report = merge_simulated_and_confirmed_positions(
        panel(SPX500=1.0, GLD=2.0), [opened(quantity=0.684)]
    )
    assert merged.iloc[-1]["GLD"] == pytest.approx(2.0)
    assert report["unconfirmed"] == ["GLD"]


def test_an_instrument_the_run_never_traded_is_added():
    """You are allowed to trade something the system did not suggest."""
    merged, report = merge_simulated_and_confirmed_positions(
        panel(SPX500=1.0), [opened("GLD", quantity=3.0, price=160.0)]
    )
    assert merged.iloc[-1]["GLD"] == pytest.approx(3.0)
    assert report["added"] == ["GLD"]


def test_a_matching_confirmation_is_not_reported_as_an_override():
    """You did exactly what was suggested. Nothing changed."""
    _, report = merge_simulated_and_confirmed_positions(
        panel(SPX500=0.684), [opened(quantity=0.684)]
    )
    assert report["overridden"] == {}


def test_no_confirmations_leaves_the_panel_alone():
    simulated = panel(SPX500=1.0)
    merged, report = merge_simulated_and_confirmed_positions(simulated, [])
    assert merged.equals(simulated)
    assert report["overridden"] == {}


def test_confirmations_with_no_simulation_still_make_a_book():
    """A run that never happened does not stop you recording a trade."""
    merged, report = merge_simulated_and_confirmed_positions(None, [opened()])
    assert merged.iloc[-1]["SPX500"] == pytest.approx(0.684)
    assert report["added"] == ["SPX500"]


def test_a_confirmed_close_flattens_the_simulated_position():
    merged, _ = merge_simulated_and_confirmed_positions(
        panel(SPX500=1.0),
        [Confirmation("SPX500", "close", price=4450.0, timestamp=NOW)],
    )
    assert merged.iloc[-1]["SPX500"] == 0.0


def test_deviations_only_cover_what_was_recommended():
    table = deviations(
        [
            opened(recommended_price=4430.0, recommended_quantity=0.7),
            opened("GLD", price=160.0, quantity=1.0),
        ]
    )
    assert list(table["ticker"]) == ["SPX500"]
    assert bool(table["partial_fill"].iloc[0]) is True


# --------------------------------------------------------------------------- #
# The reply parser
# --------------------------------------------------------------------------- #
def parse(body: str):
    return reply_module.parse_confirmation_email(body, timestamp=NOW)


def test_a_reply_is_read():
    commands = parse("CONFIRM OPEN SPX500 PRICE=4434.8 QTY=0.684")

    assert len(commands) == 1 and commands[0].ok
    confirmation = commands[0].confirmation
    assert confirmation.ticker == "SPX500"
    assert confirmation.price == pytest.approx(4434.8)
    assert confirmation.quantity == pytest.approx(0.684)
    assert confirmation.source == "email"


def test_a_quoted_recommendation_is_not_a_confirmation():
    """The property the strictness is for.

    A forwarded alert carries the original instruction, which reads exactly
    like a confirmation to a relaxed parser — and would silently rewrite the
    book with a size nobody filled.
    """
    body = (
        "Took the first one.\n"
        "CONFIRM OPEN SPX500 PRICE=4434.8 QTY=0.684\n"
        "\n"
        "> On 25 Aug 2026, CFDInvest wrote:\n"
        "> CONFIRM OPEN TLT PRICE=231.95 QTY=999\n"
    )
    parsed = [c.confirmation for c in parse(body) if c.ok]
    assert [c.ticker for c in parsed] == ["SPX500"]


@pytest.mark.parametrize(
    "separator",
    [
        "On 25 Aug 2026, at 08:00, CFDInvest <hello@example.com> wrote:",
        "-----Original Message-----",
        "---------- Forwarded message ----------",
        "From: CFDInvest <hello@example.com>",
        "Begin forwarded message:",
        "________________________________",
    ],
)
def test_nothing_below_a_quote_separator_is_read(separator):
    """The hole a prefix check alone leaves open.

    Outlook and Apple Mail drop the original message in below a separator
    without prefixing a single line of it. A forwarded alert down there
    carries a real CONFIRM line — reading it as a fresh instruction would
    rewrite the book with a fill nobody made.
    """
    body = (
        "Took the first one.\n"
        "CONFIRM OPEN SPX500 PRICE=4434.8 QTY=0.684\n"
        f"\n{separator}\n"
        "CONFIRM OPEN TLT PRICE=231.95 QTY=999\n"
    )
    parsed = [c.confirmation for c in parse(body) if c.ok]
    assert [c.ticker for c in parsed] == ["SPX500"]


def test_an_indented_quote_is_not_read():
    """Three spaces would slip past the anchored command pattern."""
    assert parse("   > CONFIRM OPEN TLT PRICE=231.95 QTY=999") == []


def test_prose_around_the_command_is_ignored():
    commands = parse(
        "Morning,\n\nCONFIRM CLOSE NAS100 PRICE=18923.4\n\nThanks\nBrent"
    )
    assert len(commands) == 1 and commands[0].ok


def test_a_direction_can_be_given_in_the_reply():
    command = parse("CONFIRM OPEN GLD PRICE=160.25 QTY=1.5 SIDE=short")[0]
    assert command.confirmation.quantity == pytest.approx(-1.5)


def test_a_thousands_separator_and_a_currency_symbol_are_tolerated():
    """What a person types, and what they paste out of the ticket."""
    command = parse("CONFIRM CLOSE UKX PRICE=£1,234.50")[0]
    assert command.confirmation.price == pytest.approx(1234.50)


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("CONFIRM OPEN SPX500 PRICE=abc QTY=1", "not a number"),
        ("CONFIRM OPEN SPX500 QTY=1", "needs a fill price"),
        ("CONFIRM HEDGE SPX500 PRICE=1", "unknown action"),
        ("CONFIRM OPEN SPX500 PRICE=1 WIBBLE=2", "unknown field"),
        ("CONFIRM OPEN SPX500 PRICE=1 PRICE=2", "given twice"),
        ("CONFIRM OPEN SPX500 PRICE=0 QTY=1", "must be > 0"),
    ],
)
def test_an_ambiguous_command_is_refused_not_guessed(line, fragment):
    command = parse(line)[0]
    assert not command.ok
    assert fragment in command.error


def test_a_body_with_no_commands_yields_nothing():
    assert parse("Thanks, will do.") == []


def test_the_parser_reaches_no_network():
    """It parses text. Fetching mail is not SMTP, and is not done here."""
    import ast

    source = Path("cfdinvest/confirm/reply.py").read_text(encoding="utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [
        module
        for module in imported
        if any(
            part in module
            for part in ("imaplib", "poplib", "smtplib", "socket", "requests", "urllib")
        )
    ]


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #
def run_cli(argv, lake) -> int:
    from cfdinvest.confirm import cli

    return cli.main(["--data-root", str(lake.paths.data_root), *argv])


def test_the_cli_records_an_open(lake, capsys):
    assert run_cli(
        ["open", "SPX500", "--price", "4434.8", "--quantity", "0.684"], lake
    ) == 0
    found = load_confirmations(lake)
    assert len(found) == 1 and found[0].action == "open"


def test_the_cli_shows_the_recommendation_before_it_writes(lake, capsys):
    """The check is worthless after the write."""
    run_cli(["open", "SPX500", "--price", "4434.8", "--quantity", "0.684"], lake)
    printed = capsys.readouterr().out

    assert printed.index("Recommended for") < printed.index("Confirmed open")


def test_the_cli_refuses_a_bad_fill(lake, capsys):
    assert run_cli(["open", "SPX500", "--price", "0", "--quantity", "1"], lake) == 1
    assert load_confirmations(lake) == []


def test_the_cli_records_a_short(lake):
    run_cli(
        ["open", "GLD", "--price", "160.25", "--quantity", "1.5",
         "--direction", "short"],
        lake,
    )
    assert load_confirmations(lake)[0].quantity == pytest.approx(-1.5)


def test_the_cli_records_a_close(lake):
    run_cli(["close", "NAS100", "--price", "18923.4"], lake)
    assert load_confirmations(lake)[0].action == "close"


def test_the_cli_records_a_flip(lake):
    run_cli(
        ["flip", "SPX500", "--close-price", "4434.8", "--open-price", "4436.1",
         "--quantity", "0.5", "--direction", "short"],
        lake,
    )
    confirmation = load_confirmations(lake)[0]
    assert confirmation.close_price == pytest.approx(4434.8)
    assert confirmation.quantity == pytest.approx(-0.5)


def test_the_cli_records_an_ignore(lake):
    run_cli(["ignore", "GLD", "--notes", "spread too wide"], lake)
    assert load_confirmations(lake)[0].notes == "spread too wide"


def test_show_prints_and_writes_nothing(lake):
    assert run_cli(["show", "SPX500"], lake) == 0
    assert load_confirmations(lake) == []


def test_listing_nothing_says_what_that_means(lake, capsys):
    """"Nothing confirmed" must not read as "no trades"."""
    run_cli(["list"], lake)
    printed = capsys.readouterr().out

    assert "Nothing has been confirmed" in printed
    assert "not the same as no trades" in printed


def test_a_reply_file_is_parsed_but_not_written_without_apply(lake, tmp_path):
    body = tmp_path / "reply.txt"
    body.write_text("CONFIRM CLOSE SPY PRICE=55.9\n", encoding="utf-8")

    assert run_cli(["from-reply", "--file", str(body)], lake) == 0
    assert load_confirmations(lake) == []


def test_a_reply_file_is_written_with_apply(lake, tmp_path):
    body = tmp_path / "reply.txt"
    body.write_text("CONFIRM CLOSE SPY PRICE=55.9\n", encoding="utf-8")

    assert run_cli(["from-reply", "--file", str(body), "--apply"], lake) == 0
    assert len(load_confirmations(lake)) == 1


def test_one_bad_line_writes_none_of_them(lake, tmp_path):
    """The sender believes both were recorded."""
    body = tmp_path / "reply.txt"
    body.write_text(
        "CONFIRM CLOSE SPY PRICE=55.9\nCONFIRM OPEN GLD PRICE=oops QTY=1\n",
        encoding="utf-8",
    )

    assert run_cli(["from-reply", "--file", str(body), "--apply"], lake) == 1
    assert load_confirmations(lake) == []


# --------------------------------------------------------------------------- #
# The recommendation, gathered
# --------------------------------------------------------------------------- #
def test_the_account_scale_is_the_ratio_of_the_two_equities():
    assert account_scale(100_000.0, 2_000.0) == pytest.approx(0.02)


@pytest.mark.parametrize(
    "equity, balance", [(None, 2_000.0), (100_000.0, None), (0.0, 2_000.0)]
)
def test_an_unknown_scale_is_absent_not_one(equity, balance):
    """A scale of 1.0 hands you the simulation's size at a hundredfold gap."""
    assert account_scale(equity, balance) is None


def test_a_recommendation_with_no_run_says_so(lake):
    result = recommendation_for("SPX500", lake)
    assert result["available"] is False
    assert "run" in result["absent"]


# --------------------------------------------------------------------------- #
# The runner, and everything downstream
# --------------------------------------------------------------------------- #
@pytest.fixture
def recorded_lake(tmp_path):
    """A lake with one completed run, written by the real scheduler."""
    from tests.test_analytics import TICKERS, build_lake
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = build_lake(tmp_path / "data", bars=260)

    def run(now="2026-08-25 07:00"):
        summary = run_scheduled(
            list(TICKERS),
            RunConfig(backtest=BacktestConfig(interval="1d")),
            repository=DataRepository(config),
            now=pd.Timestamp(now, tz="UTC"),
        )
        assert summary.status == "ok", summary.error
        return summary.run_id

    return config, run


def test_a_run_with_no_confirmations_writes_no_overlay(recorded_lake):
    """An empty overlay would read as "confirmed exactly as simulated"."""
    from cfdinvest.persist import readers

    config, run = recorded_lake
    run_id = run()

    _, source = readers.load_positions(run_id, config)
    assert source == "simulated"
    assert readers.load_frame("positions_confirmed", run_id, config).empty


def test_a_confirmed_position_reaches_the_next_run(recorded_lake):
    from cfdinvest.persist import readers

    config, run = recorded_lake
    run()
    write_confirmation(
        Confirmation.of("GLD", "open", direction="short", quantity=1.5,
                        price=160.25),
        config,
    )
    run_id = run("2026-08-25 08:00")

    positions, source = readers.load_positions(run_id, config)
    assert source == "confirmed"
    assert positions.iloc[-1]["GLD"] == pytest.approx(-1.5)


def test_the_simulated_artefact_is_left_intact(recorded_lake):
    """Two files, both true. Overwriting one loses the comparison."""
    from cfdinvest.persist import readers

    config, run = recorded_lake
    run()
    write_confirmation(
        Confirmation.of("GLD", "open", direction="short", quantity=1.5,
                        price=160.25),
        config,
    )
    run_id = run("2026-08-25 08:00")

    simulated, _ = readers.load_positions(run_id, config, confirmed=False)
    confirmed, _ = readers.load_positions(run_id, config, confirmed=True)

    assert simulated.iloc[-1]["GLD"] == pytest.approx(0.0)
    assert confirmed.iloc[-1]["GLD"] == pytest.approx(-1.5)
    assert simulated.iloc[:-1].equals(confirmed.iloc[:-1])


def test_analytics_score_the_confirmed_book(recorded_lake):
    from cfdinvest.analytics.persisted import execution_frame_from_run

    config, run = recorded_lake
    run()
    assert execution_frame_from_run(config).metadata["positions_source"] == (
        "simulated"
    )

    write_confirmation(
        Confirmation.of("GLD", "open", direction="short", quantity=1.5,
                        price=160.25),
        config,
    )
    run("2026-08-25 08:00")
    frame = execution_frame_from_run(config)

    assert frame.metadata["positions_source"] == "confirmed"
    assert frame.positions.iloc[-1]["GLD"] == pytest.approx(-1.5)


def test_a_confirmation_failure_does_not_fail_the_run(recorded_lake, monkeypatch):
    """Recording a completed run must not be able to destroy it."""
    from cfdinvest.persist import confirm as confirm_module

    config, run = recorded_lake
    monkeypatch.setattr(
        confirm_module, "load_confirmations",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk went away")),
    )
    assert run() is not None


def test_the_recommendation_reads_a_real_run(recorded_lake):
    config, run = recorded_lake
    run()

    from cfdinvest.confirm.recommendation import recommended_tickers

    tickers = recommended_tickers(config)
    assert tickers

    result = recommendation_for(tickers[0], config, account_balance=2_000.0)
    assert result["available"] is True
    assert result["scale"] == pytest.approx(2_000.0 / 100_000.0)


# --------------------------------------------------------------------------- #
# The alert says how to reply
# --------------------------------------------------------------------------- #
def test_the_trade_alert_carries_a_confirm_line():
    """The moment you know your fill is the moment you read the alert."""
    from cfdinvest.schedule.notify import build_trade_alert

    body = build_trade_alert(
        {"asset": "SPX500", "action": "open_long", "fill_price": 4434.8}
    ).get_content()

    assert "CONFIRM OPEN SPX500" in body
    assert "QTY=" in body


def test_a_close_alert_asks_for_no_quantity():
    from cfdinvest.schedule.notify import build_trade_alert

    body = build_trade_alert(
        {"asset": "NAS100", "action": "close", "fill_price": 18_923.4}
    ).get_content()

    assert "CONFIRM CLOSE NAS100" in body
    assert "QTY=" not in body


def test_the_line_in_an_alert_parses_back(recorded_lake):
    """The instruction the email gives is one the parser accepts."""
    from cfdinvest.schedule.notify import build_trade_alert

    body = build_trade_alert(
        {"asset": "SPX500", "action": "open_long", "fill_price": 4434.8}
    ).get_content()
    template = [
        line for line in body.splitlines() if line.strip().startswith("CONFIRM")
    ][0]
    filled = template.replace("<your fill>", "4434.8").replace("<your size>", "0.684")

    command = reply_module.parse_confirmation_line(filled.strip())
    assert command.ok, command.error
    assert command.confirmation.ticker == "SPX500"
