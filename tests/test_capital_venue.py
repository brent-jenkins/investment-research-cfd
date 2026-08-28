"""Pricing the shadow book on Capital's terms instead of Trading 212's.

The book and the demo account are meant to hold the same intended positions,
so the gap between them measures how wrong the *simulation* is. While the
book charged one broker and the account paid another, that gap measured two
things at once and neither could be read off it.

M340 a row that does not say whose terms it was struck on
M341 a measured spread written into the record as a tabled one
M342 one row struck on two venues -- Capital's carry, T212's spread estimate
M343 a short charged the long financing rate
M344 the live flag written after the outcome instead of at the decision
M345 financing invented when the terms file is missing
M346 an unknown instrument answering "no constraint" instead of "not known"
M347 the book acquiring a broker import
"""

from __future__ import annotations

import pytest

from cfdinvest.advisor import capital_venue, papertrade

QUOTE = {"AAPL": {"bid": 100.0, "offer": 100.2, "currency": "USD"}}

TERMS = {
    "source": {"read_on": "2026-08-28"},
    "financing": {"long_annual": 0.0786, "short_annual": 0.00249,
                  "financing_verified": False},
    "margin": {"implied_leverage": 5.0},
    "min_deal_size": {"AAPL": 0.1},
}


def table(quotes=None, terms=None):
    return capital_venue.rules_from_quotes(quotes or QUOTE, terms or TERMS)


def struck(direction="long", rules=None, ticker="AAPL", price=100.1, **kwargs):
    return papertrade.open_trade(
        ticker, direction, price, "2026-08-28T15:00:00+00:00",
        rules=rules if rules is not None else table(),
        fx_rate=0.79, cohort="c35", venue=capital_venue.VENUE, **kwargs,
    )


def test_every_row_says_whose_terms_it_was_struck_on():
    """M340: a record that silently mixes two cost bases.

    The book charged Trading 212 from 2026-08-26 and Capital from
    2026-08-28. Rows either side are not comparable trade for trade, and
    without the stamp nothing downstream can tell them apart.
    """
    row = struck().as_row()

    assert row["venue"] == "capital"
    assert row["schema_version"] == papertrade.SCHEMA_VERSION
    assert papertrade.SCHEMA_VERSION >= 2, (
        "the venue switch must bump the schema; version 1 rows carry no "
        "venue at all and a reader has to assume Trading 212"
    )


def test_a_measured_spread_is_not_recorded_as_a_tabled_one():
    """M341: three different claims flattened into two.

    "measured" (a live quote at the moment of the decision), "recorded" (a
    broker's published figure) and "estimated" (an asset-class table) are
    different evidence. This project's entire cost history is a record of
    that distinction mattering.
    """
    row = struck().as_row()

    assert row["spread_source"] == "measured"
    # 0.20 of spread on 100.1, at £1,000 notional and 0.79 to the dollar.
    assert row["spread_cost_gbp"] == pytest.approx(
        0.20 * (1000.0 / (100.1 * 0.79)) * 0.79, rel=1e-6
    )


def test_an_unquoted_instrument_is_not_priced_on_two_venues(monkeypatch):
    """M342: Capital's carry beside a Trading 212 spread estimate.

    A venue that quotes live on everything it deals says something by
    quoting nothing: the instrument is not dealable there. Falling through
    to another broker's class table would put two brokers in one row.
    """
    quiet = table({"ZZZZ": {"bid": None, "offer": None}})
    entry = quiet.get("ZZZZ")

    assert entry.available is False
    assert entry.leverage is None, "leverage survived a missing quote"
    assert entry.financing_rate is None, "financing survived a missing quote"
    assert entry.spread_typical is None

    row = struck(ticker="ZZZZ", rules=quiet).as_row()
    assert row["spread_source"] == "", (
        f"fell through to a table estimate ({row['spread_source']}) for an "
        "instrument the venue does not quote"
    )
    assert row["spread_cost_gbp"] is None
    assert set(row["missing"]) >= {"leverage", "financing_rate"}, (
        "the absent terms were not named"
    )


def test_the_trading_212_table_still_estimates_for_its_own_instruments():
    """The guard must not have switched off the estimate everywhere.

    Trading 212 publishes a spread for six of forty instruments, so most
    paper trades would otherwise pay none -- and an unpaid cost is a worse
    error than an estimated one.
    """
    class T212ish:
        ticker = "AAPL"
        spread_typical = None
        leverage = 5.0
        currency = "USD"
        fx_fee_applies = True
        min_trade_value = None

        def annual_financing(self, direction=None):
            return 0.2533

    class Table:
        def get(self, ticker):
            return T212ish()

    row = papertrade.open_trade(
        "AAPL", "long", 100.1, "2026-08-28T15:00:00+00:00",
        rules=Table(), fx_rate=0.79, venue="t212",
    ).as_row()

    assert row["spread_source"] == "estimated", (
        "the Trading 212 fallback stopped estimating; most of its book "
        "would now trade for free"
    )
    assert row["spread_cost_gbp"] is not None


def test_each_side_is_charged_its_own_financing_rate():
    """M343: a short charged the long rate.

    Capital charges 7.86%/yr long and 0.249% short -- a factor of thirty.
    Baking one in when the table is built misprices every trade on the
    other side.
    """
    assert struck("long").as_row()["financing_rate"] == pytest.approx(0.0786)
    assert struck("short").as_row()["financing_rate"] == pytest.approx(0.00249)


def test_an_unnamed_side_is_charged_the_dearer_rate():
    entry = table().get("AAPL")
    assert entry.annual_financing(None) == pytest.approx(0.0786)
    assert entry.annual_financing("") == pytest.approx(0.0786)


def test_the_live_flag_is_the_decision_not_the_outcome():
    """M344: a row edited after the broker replied.

    The book's whole worth is being a record nobody could have edited once
    the outcome was known. "Was live armed when this was decided" is
    knowable up front and never changes; "did the broker place it" is not,
    and lives in the order store.
    """
    assert struck(live_armed=True).as_row()["live_armed"] is True
    assert struck(live_armed=False).as_row()["live_armed"] is False
    # Absent is its own answer: not knowing and knowing it was off are
    # different statements, and only one should read as reassurance.
    assert struck().as_row()["live_armed"] is None

    fields = {f for f in struck().as_row()}
    assert not {f for f in fields if "placed" in f or "filled" in f}, (
        "the book records what the broker did; that belongs in the order store"
    )


def test_the_venue_is_carried_from_the_open_not_re_derived_on_close():
    """A position opened on one venue's terms is closed on them.

    Otherwise switching venue rewrites the cost basis of trades already
    running, which is editing history by another route.
    """
    opened = struck().as_row()
    opened["opened_at"] = opened["at"]
    closed = papertrade.value_trade(
        opened, 101.0, "2026-08-29T15:00:00+00:00", live_armed=True
    ).as_row()

    assert closed["venue"] == "capital"

    legacy = dict(opened, venue="t212")
    assert papertrade.value_trade(
        legacy, 101.0, "2026-08-29T15:00:00+00:00"
    ).as_row()["venue"] == "t212", "a Trading 212 position was closed as Capital"


def test_missing_terms_refuse_rather_than_invent_a_rate(tmp_path):
    """M345: a plausible cost attributable to nothing."""
    with pytest.raises(FileNotFoundError, match="refusing to invent"):
        capital_venue.load_capital_terms(tmp_path / "absent.toml")


def test_the_shipped_terms_still_say_financing_is_unconfirmed():
    terms = capital_venue.load_capital_terms()
    assert terms["financing"]["long_annual"] == pytest.approx(0.0786, rel=1e-3)
    assert terms["financing"]["financing_verified"] is False, (
        "financing is now marked verified; update the docstrings that call "
        "it corroborated-but-unconfirmed"
    )
    assert table().financing_verified is False


def test_an_unknown_instrument_says_not_known_not_unconstrained():
    """M346: an empty answer read as permission."""
    entry = table().get("NOPE")
    assert entry is not None, "None would eventually be treated as no constraint"
    assert entry.ticker == "NOPE"
    assert entry.leverage is None and entry.financing_rate is None
    assert entry.annual_financing("long") is None


def test_the_venue_module_names_no_broker_and_imports_none():
    """M347: the simulation acquiring a path that can place an order."""
    import ast
    from pathlib import Path

    source = Path("cfdinvest/advisor/capital_venue.py").read_text(encoding="utf-8")
    assert "dealing" not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "broker" not in node.module, "the venue imports a broker"
