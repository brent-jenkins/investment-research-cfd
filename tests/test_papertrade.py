"""A paper book that pays real Trading 212 costs.

Mutations checked (each reintroduced, each failed):

M115 the stake read as margin rather than exposure, so leverage silently
     varies the position size
     -> test_the_stake_is_exposure_not_margin
M116 a trade opened below the broker's minimum
     -> test_a_stake_under_the_brokers_minimum_is_refused
M117 the entry spread not deducted from the round trip
     -> test_the_spread_paid_on_entry_is_charged_to_the_round_trip
M118 financing ignored on a closed position
     -> test_a_position_held_for_weeks_pays_to_have_been_held
M119 a duplicate open treated as a second position
     -> test_a_second_open_is_a_correction_not_another_position
"""

from __future__ import annotations

import pytest

from cfdinvest.advisor import papertrade as module
from cfdinvest.livehelper.t212_rules import rules_from_mapping


@pytest.fixture
def rules():
    """A share at 5x and an index at 20x -- the extremes of the real table."""
    return rules_from_mapping(
        {
            "LULU": {
                "t212_name": "Lululemon", "leverage": 5.0, "currency": "USD",
                "financing_daily_pct_long": 0.0694,
                "financing_daily_pct_short": 0.0251,
                "fx_fee_applies": True, "min_trade_value": 5.0,
                "spread_typical": 0.20,
            },
            "^GSPC": {
                "t212_name": "USA 500", "leverage": 20.0, "currency": "USD",
                "financing_daily_pct_long": 0.0083,
                "financing_daily_pct_short": 0.0139,
                "fx_fee_applies": True, "min_trade_value": 20.0,
            },
            "GBPUSD=X": {
                "t212_name": "GBPUSD", "leverage": 30.0, "currency": "USD",
                "financing_daily_pct_long": 0.0153,
                "financing_daily_pct_short": 0.0106,
                "fx_fee_applies": True, "min_trade_value": 100.0,
            },
        },
        "test",
    )


def test_the_stake_is_exposure_not_margin(rules):
    """M115: leverage varying the stake without anyone choosing it.

    With a fixed *margin* a 30x FX pair would carry £30,000 of exposure
    against a 5x share's £5,000, and the currency trades would dominate the
    result on position size alone. Fixed exposure means the P&L is 1,000
    times the move whatever the instrument, and only the call varies.
    """
    share = module.open_trade("LULU", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules)
    index = module.open_trade("^GSPC", "long", 5000.0, "2026-08-26T12:00:00Z", rules=rules)
    pair = module.open_trade("GBPUSD=X", "long", 1.27, "2026-08-26T12:00:00Z", rules=rules)

    for trade in (share, index, pair):
        assert trade is not None
        assert trade.notional_gbp == pytest.approx(module.STAKE_GBP)

    # Margin is what varies, and it varies the right way: more leverage,
    # less margin for the same exposure.
    assert share.margin_gbp == pytest.approx(1000.0 / 5.0)
    assert index.margin_gbp == pytest.approx(1000.0 / 20.0)
    assert pair.margin_gbp == pytest.approx(1000.0 / 30.0)


def test_a_stake_under_the_brokers_minimum_is_refused(rules):
    """M116: a trade the real account could not have taken.

    Minimums vary by instrument type by a factor of twenty -- £5 on a
    share, £100 on an FX pair. A paper trade below one teaches nothing,
    because it could never have happened.
    """
    assert module.open_trade(
        "GBPUSD=X", "long", 1.27, "2026-08-26T12:00:00Z", rules=rules, stake=50.0
    ) is None
    # The same stake clears the share's £5 floor.
    assert module.open_trade(
        "LULU", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules, stake=50.0
    ) is not None


def test_the_spread_paid_on_entry_is_charged_to_the_round_trip(rules):
    """M117: the spread recorded and then forgotten at the close.

    Spread was £15,562 of the backtest's costs. A paper book that recorded
    it on entry and left it out of the result would report a profit the
    account never had.
    """
    opened = module.open_trade(
        "LULU", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.79
    )
    assert opened.spread_cost_gbp is not None and opened.spread_cost_gbp > 0

    closed = module.value_trade(opened.as_row(), 100.0, "2026-08-27T12:00:00Z")
    # Flat on price, so everything left is cost -- and the spread must be
    # part of it.
    assert closed.net_pnl_gbp is not None
    assert closed.net_pnl_gbp <= -opened.spread_cost_gbp


def test_a_position_held_for_weeks_pays_to_have_been_held(rules):
    """M118: financing dropped on the close.

    A long share CFD carries at 25.33% a year. Over thirty days that is
    roughly 2% of the position -- the single largest cost line in the whole
    backtest, and invisible if it is not charged.
    """
    opened = module.open_trade(
        "LULU", "long", 100.0, "2026-07-27T12:00:00Z", rules=rules, fx_rate=0.79
    )
    brief = module.value_trade(opened.as_row(), 100.0, "2026-07-28T12:00:00Z")
    long_held = module.value_trade(opened.as_row(), 100.0, "2026-08-26T12:00:00Z")

    assert brief.financing_gbp is not None and long_held.financing_gbp is not None
    assert long_held.financing_gbp > brief.financing_gbp * 20
    assert long_held.net_pnl_gbp < brief.net_pnl_gbp


def test_a_short_profits_when_the_price_falls(rules):
    """The sign that would make every short read backwards."""
    opened = module.open_trade(
        "LULU", "short", 100.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.79
    )
    closed = module.value_trade(opened.as_row(), 90.0, "2026-08-27T12:00:00Z")

    assert closed.gross_pnl_gbp is not None and closed.gross_pnl_gbp > 0
    assert closed.net_pct is not None and closed.net_pct > 0


def test_a_second_open_is_a_correction_not_another_position(rules):
    """M119: two opens read as two positions.

    Same rule the confirm layer uses. Without it a re-recorded entry
    doubles the exposure in the replay and the book reports a position size
    nobody chose.
    """
    book = [
        {"event": "open", "at": "2026-08-24T12:00:00Z", "ticker": "LULU",
         "direction": "long", "quantity": 10.0, "price": 100.0},
        {"event": "open", "at": "2026-08-25T12:00:00Z", "ticker": "LULU",
         "direction": "long", "quantity": 12.0, "price": 98.0},
        {"event": "open", "at": "2026-08-25T12:00:00Z", "ticker": "^GSPC",
         "direction": "long", "quantity": 0.2, "price": 5000.0},
    ]
    live = module.open_positions(book)

    assert set(live) == {"LULU", "^GSPC"}
    assert live["LULU"]["quantity"] == pytest.approx(12.0), "the later one wins"

    book.append({"event": "close", "at": "2026-08-26T12:00:00Z", "ticker": "LULU"})
    assert set(module.open_positions(book)) == {"^GSPC"}


def test_an_instrument_with_no_recorded_terms_is_not_invented(rules):
    """No leverage recorded means no margin figure, not a guessed one."""
    trade = module.open_trade(
        "UNKNOWN", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules
    )
    assert trade is not None, "it can still be tracked"
    assert trade.margin_gbp is None
    assert "leverage" in trade.missing


# --------------------------------------------------------------------------- #
# One pass of the book
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def recheck_approves(monkeypatch):
    """Let the last-look pass, for every test that is not about the last look.

    Opening now runs the recorded signal past a fresh re-evaluation, and
    that re-evaluation reads the lake. These tests hand the book synthetic
    signals with no lake behind them, so the real check would decline
    everything -- correctly, since a check that cannot run is not a check
    that passed, but it would turn every assertion in this file into a test
    of the same one gate.

    The tests that *are* about the gate patch this again with their own
    behaviour, which wins because it is applied second.
    """
    from cfdinvest.advisor import livecheck

    monkeypatch.setattr(livecheck, "holds", lambda *a, **k: (True, ""))


def _signal(ticker, direction="long", confidence=0.9, **extra):
    return {"ticker": ticker, "direction": direction, "confidence": confidence, **extra}


def test_there_is_no_cap_on_how_many_positions_may_be_open(tmp_path, monkeypatch):
    """M120: a position limit nobody asked for.

    The book is a test of signal quality, not of portfolio construction.
    Capping it would silently decide which calls get measured -- and the
    ones dropped would be the ones that arrived last, which is arbitrary.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)

    tickers = [f"T{i:02d}" for i in range(25)]
    signals = [_signal(t) for t in tickers]
    prices = {t: 100.0 for t in tickers}

    result = papertrade_run.run_paper_pass(
        signals, prices, now="2026-08-26T12:00:00Z", blackout_days=0
    )
    assert result["opened"] == 25, "every qualifying call must be taken"


def test_only_one_position_per_instrument(tmp_path, monkeypatch):
    """M121: a second position stacked on the first.

    Two open trades in one instrument would double its weight in the
    result without anyone choosing that, and the second would be scored as
    though it were an independent call.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)

    signals = [_signal("AAA")]
    prices = {"AAA": 100.0}

    first = papertrade_run.run_paper_pass(
        signals, prices, now="2026-08-26T12:00:00Z", blackout_days=0
    )
    second = papertrade_run.run_paper_pass(
        signals, prices, now="2026-08-26T18:00:00Z", blackout_days=0
    )

    assert first["opened"] == 1
    assert second["opened"] == 0
    assert second["skipped"].get("already open") == 1


def test_an_instrument_just_closed_is_left_alone(tmp_path, monkeypatch):
    """M122: the cooldown ignored on the paper book.

    Trips reopened within 21 days lost £1,489 across 145 of them; those
    opened after a longer gap made £19,954 across 512. The book has to obey
    the rule it is testing or it is testing something else.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(papertrade, "_path", lambda config=None, account=None: path)
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)

    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(
        json.dumps({"event": "close", "at": "2026-08-20T12:00:00Z",
                    "ticker": "AAA", "direction": "long"}) + "\n",
        encoding="utf-8",
    )

    soon = papertrade_run.run_paper_pass(
        [_signal("AAA")], {"AAA": 100.0},
        now="2026-08-26T12:00:00Z", blackout_days=0, cooldown_days=14,
    )
    assert soon["opened"] == 0
    assert soon["skipped"].get("cooling off") == 1

    later = papertrade_run.run_paper_pass(
        [_signal("AAA")], {"AAA": 100.0},
        now="2026-09-10T12:00:00Z", blackout_days=0, cooldown_days=14,
    )
    assert later["opened"] == 1


def test_nothing_opens_while_the_market_is_shut(tmp_path, monkeypatch):
    """A fill at a price nobody was quoting is not a fill."""
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: False)

    result = papertrade_run.run_paper_pass(
        [_signal("AAA")], {"AAA": 100.0},
        now="2026-08-26T02:00:00Z", blackout_days=0,
    )
    assert result["opened"] == 0
    assert result["skipped"].get("market closed") == 1


def test_vix_is_a_signal_and_never_a_position(tmp_path, monkeypatch):
    """It carries no volume and Trading 212 offers no instrument for it."""
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)

    result = papertrade_run.run_paper_pass(
        [_signal("^VIX"), _signal("AAA")], {"^VIX": 20.0, "AAA": 100.0},
        now="2026-08-26T12:00:00Z", blackout_days=0,
    )
    assert result["opened"] == 1
    assert result["skipped"].get("not an instrument") == 1


def test_nothing_opens_into_a_reporting_date(tmp_path, monkeypatch):
    """The expected move is 8.9% and the direction is a coin flip."""
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    monkeypatch.setattr(
        "cfdinvest.ingestion.earnings.load_earnings_dates",
        lambda config=None, account=None: {"AAA": ["2026-08-27"]},
    )

    result = papertrade_run.run_paper_pass(
        [_signal("AAA")], {"AAA": 100.0},
        now="2026-08-26T12:00:00Z", blackout_days=2,
    )
    assert result["opened"] == 0
    assert result["skipped"].get("reports soon") == 1


def test_every_opened_trade_records_the_state_it_was_decided_in(tmp_path, monkeypatch):
    """M126: the decision recorded without the evidence around it.

    Every price-derived feature tested against 2,018 historical round trips
    collapsed to |t|<1 once the trend engine's confidence was accounted for.
    Sentiment, the only candidate not made of price, could not be tested at
    all: two years of verified headlines overlapped 37 of those trips.

    Recording the state at entry is the only thing that makes the question
    answerable later, so a trade written without it is a lost observation.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(papertrade, "_path", lambda config=None, account=None: path)
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    monkeypatch.setattr(
        papertrade_run, "_sentiment_for",
        lambda ticker: {"sentiment": -0.25, "sentiment_n": 40},
    )

    # Volume and ATR come from the analysis panel, not the signal row --
    # the signals frame carries only direction, size, confidence and a
    # reason, so anything read off the row would record as absent forever.
    monkeypatch.setattr(
        papertrade_run, "_risk_state",
        lambda config=None, account=None: {"AAA": {"volume_ratio": 1.4, "atr_pct": 2.3}},
    )

    signals = [{
        "ticker": "AAA", "direction": "long", "confidence": 0.82,
        "reason": "trend_long @risk_on",
    }]
    papertrade_run.run_paper_pass(
        signals, {"AAA": 100.0}, now="2026-08-26T12:00:00Z", blackout_days=0
    )

    opened = [r for r in papertrade.load_book() if r["event"] == "open"]
    assert len(opened) == 1
    row = opened[0]
    assert row["confidence"] == pytest.approx(0.82)
    assert row["sentiment"] == pytest.approx(-0.25)
    assert row["sentiment_n"] == 40
    assert row["volume_ratio"] == pytest.approx(1.4)
    assert row["atr_pct"] == pytest.approx(2.3)
    assert row["regime"] == "risk_on"


def test_a_missing_reading_is_absent_rather_than_zero(tmp_path, monkeypatch):
    """Nothing measured and measured-as-zero are different statements.

    A sentiment of 0.0 means the coverage balanced. None means the feed
    could not be read. Storing the second as the first would put invented
    neutrality into the very dataset built to settle whether sentiment
    matters.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(papertrade, "_path", lambda config=None, account=None: path)
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    monkeypatch.setattr(
        papertrade_run, "_sentiment_for",
        lambda ticker: {"sentiment": None, "sentiment_n": None},
    )

    papertrade_run.run_paper_pass(
        [{"ticker": "AAA", "direction": "long", "confidence": 0.9}],
        {"AAA": 100.0}, now="2026-08-26T12:00:00Z", blackout_days=0,
    )
    row = [r for r in papertrade.load_book() if r["event"] == "open"][0]

    assert row["sentiment"] is None
    assert row["volume_ratio"] is None
    assert row["filings_90d"] is None


def test_the_three_arms_do_not_block_each_other(tmp_path, monkeypatch):
    """M127: one cohort's position blocking another's.

    Three books at three thresholds only answer the question if they are
    independent. If a position held in the 0.50 arm stopped the 0.70 arm
    from opening, the comparison would measure whichever arm happened to
    fire first rather than the thresholds.
    """
    from cfdinvest.advisor import papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    monkeypatch.setattr(
        papertrade_run, "_sentiment_for", lambda t: {"sentiment": None, "sentiment_n": None}
    )

    # Confidence 0.65 clears 0.50 and 0.60, and misses 0.70.
    signals = [{"ticker": "AAA", "direction": "long", "confidence": 0.65}]
    results = papertrade_run.run_all_cohorts(
        signals, {"AAA": 100.0}, now="2026-08-26T12:00:00Z", blackout_days=0
    )

    # Confidence 0.65 clears the baseline arm and 0.50 and 0.60; it misses
    # 0.70. The baseline is whatever the engine enters on rather than a
    # round number -- it was 0.35 when this was written, and the arms are
    # derived so that changing it cannot leave the forward record testing a
    # strategy nobody trades.
    opened = {r["cohort"]: r["opened"] for r in results}
    assert opened == {"c35": 1, "c50": 1, "c60": 1, "c70": 0}

    book = papertrade.load_book()
    assert {r["cohort"] for r in book if r["event"] == "open"} == {
        "c35", "c50", "c60"
    }
    # And each arm sees only its own position.
    assert list(papertrade.open_positions(book, "c50")) == ["AAA"]
    assert list(papertrade.open_positions(book, "c70")) == []


def test_a_dollar_instrument_is_sized_through_the_exchange_rate():
    """M128: the stake treated as if the instrument were priced in sterling.

    £1,000 of exposure in a $100 share is not 10 units. Getting this wrong
    misstates the position by the exchange rate, and every GBP figure that
    follows by its square.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import rules_from_mapping

    rules = rules_from_mapping(
        {"AAA": {"t212_name": "A", "leverage": 5.0, "currency": "USD",
                 "financing_daily_pct_long": 0.0694,
                 "financing_daily_pct_short": 0.0251,
                 "fx_fee_applies": True}},
        "test",
    )
    # 0.7874 GBP per USD, i.e. GBPUSD at 1.27.
    trade = papertrade.open_trade(
        "AAA", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874
    )

    # £1,000 / ($100 x 0.7874) = 12.70 units, not 10.
    assert trade.quantity == pytest.approx(12.70, abs=0.01)
    assert trade.notional_gbp == pytest.approx(1000.0)
    assert trade.fx_rate == pytest.approx(0.7874)


def test_the_conversion_fee_lands_on_the_result_not_the_notional():
    """0.5% of the profit, not of the position. Worth fifty times over."""
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import rules_from_mapping

    rules = rules_from_mapping(
        {"AAA": {"t212_name": "A", "leverage": 5.0, "currency": "USD",
                 "financing_daily_pct_long": 0.0694,
                 "financing_daily_pct_short": 0.0251,
                 "fx_fee_applies": True}},
        "test",
    )
    opened = papertrade.open_trade(
        "AAA", "long", 100.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874
    )
    closed = papertrade.value_trade(opened.as_row(), 110.0, "2026-08-27T12:00:00Z")

    assert closed.gross_pnl_gbp is not None and closed.gross_pnl_gbp > 0
    # 0.5% of the ~£100 gain, not of the £1,000 position.
    assert closed.fx_fee_gbp == pytest.approx(closed.gross_pnl_gbp * 0.005, rel=0.01)
    assert closed.fx_fee_gbp < 1.0


def test_an_instrument_with_no_published_spread_still_pays_one():
    """M129: the silent zero.

    Trading 212 publishes a typical spread for six of the forty
    instruments and none of the nineteen shares. Charging only what is
    published would let most paper trades cross the spread for free, and
    the backtest's central finding was that costs were 10.9x the gross
    edge. An unpaid cost flatters the result exactly where it matters.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import load_rules

    rules = load_rules("config/t212_rules.toml")

    share = papertrade.open_trade(
        "AAPL", "long", 200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874
    )
    assert share is not None
    assert share.spread_cost_gbp is not None and share.spread_cost_gbp > 0
    assert share.spread_source == "estimated"

    # And where a figure IS published it is used, and labelled as measured.
    index = papertrade.open_trade(
        "^FTSE", "long", 8200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=1.0
    )
    assert index is not None
    assert index.spread_source == "recorded"

    # The two are different claims and the book must keep them apart.
    assert share.spread_source != index.spread_source


def test_financing_differs_between_the_two_sides():
    """M132: one rate used for both directions.

    A CFD charges long and short differently -- Apple is 0.0694%/day long
    against 0.0251% short, which is nearly three times -- so a single rate
    would misprice one side of every pair. Carry is the largest cost line
    in the backtest.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import load_rules

    rules = load_rules("config/t212_rules.toml")
    opened = {
        side: papertrade.open_trade(
            "AAPL", side, 200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874
        )
        for side in ("long", "short")
    }

    rates = {side: trade.financing_rate for side, trade in opened.items()}
    assert all(r is not None for r in rates.values())
    assert rates["long"] > rates["short"]

    # And it reaches the P&L, not just the record: seven days flat costs
    # the long side materially more than the short.
    costs = {}
    for side, trade in opened.items():
        closed = papertrade.value_trade(
            trade.as_row(), 200.0, "2026-09-02T12:00:00Z"
        )
        assert closed.financing_gbp is not None
        costs[side] = closed.financing_gbp
    assert costs["long"] > costs["short"] * 2


def test_margin_follows_the_instruments_own_leverage():
    """M133: margin defaulted rather than read from the recorded table.

    £1,000 of exposure is £200 of margin on a 5x share and £50 on a 20x
    index. A single default would misstate what the account is actually
    committing on every trade that is not that default.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import load_rules

    rules = load_rules("config/t212_rules.toml")
    share = papertrade.open_trade(
        "AAPL", "long", 200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874
    )
    index = papertrade.open_trade(
        "^FTSE", "long", 8200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=1.0
    )

    assert share.margin_gbp == pytest.approx(1000.0 / share.leverage, rel=0.01)
    assert index.margin_gbp == pytest.approx(1000.0 / index.leverage, rel=0.01)
    assert share.margin_gbp > index.margin_gbp


def test_a_part_finished_session_is_not_read_as_a_quiet_market(monkeypatch):
    """M131: a partial bar compared against full-day medians.

    The scheduler runs mid-session, so the newest bar in the analysis
    panel holds only the volume traded so far that day. Measured against
    twenty full days it reads low for every instrument at once -- the
    first cut of this returned 0.08x across the whole universe, which is
    a clock artefact, not a market. Recorded as a risk reading it would
    have looked like a universe-wide collapse in participation.
    """
    import pandas as pd

    from cfdinvest.advisor import papertrade_run

    # Twenty-one ordinary sessions -- twenty to form the median and one to
    # read against it -- then a part-finished one at a fifth of a day.
    days = pd.date_range("2026-07-26", periods=22, freq="D")
    volumes = [1_000_000.0] * 21 + [200_000.0]
    closes = [100.0] * 22

    rows = []
    for stamp, volume, close in zip(days, volumes, closes):
        rows.append({"timestamp": stamp, "panel": "prices.volume",
                     "series": "AAA", "value": volume})
        rows.append({"timestamp": stamp, "panel": "prices.close",
                     "series": "AAA", "value": close})
        rows.append({"timestamp": stamp, "panel": "volatility.atr",
                     "series": "AAA", "value": 2.0})
    frame = pd.DataFrame(rows)

    from cfdinvest.persist import readers

    # A run stamped inside the final session, so that bar is partial.
    monkeypatch.setattr(readers, "load_latest_run", lambda *a, **k: "20260816T150644Z")
    monkeypatch.setattr(readers, "load_analysis_frame", lambda *a, **k: frame)

    state = papertrade_run._risk_state()

    assert "AAA" in state
    # The last full session, against the twenty before it: normal.
    assert state["AAA"]["volume_ratio"] == pytest.approx(1.0, abs=0.01)
    # Emphatically not the fifth-of-a-day the partial bar would have given.
    assert state["AAA"]["volume_ratio"] > 0.5
    # ATR still reads, as a percentage of price.
    assert state["AAA"]["atr_pct"] == pytest.approx(2.0)


def test_the_book_can_only_open_and_close_never_resize():
    """M134: a resize event, which Trading 212 cannot perform.

    The venue offers no way to change the size of an open CFD position --
    it can only be closed. A book that trimmed a winner would be recording
    a trade the account could not have made, and every cost and outcome
    after that point would belong to a position that never existed.

    Asserted against the module rather than a run, because the failure
    would arrive as a new code path and not as a bad number.
    """
    import inspect

    from cfdinvest.advisor import papertrade, papertrade_run

    import ast

    def code_only(module) -> str:
        """The module with its prose stripped.

        The docstrings explain why there is no resize, so a plain text
        search matches its own explanation.
        """
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
        return ast.unparse(tree)

    for module in (papertrade, papertrade_run):
        source = code_only(module)
        assert "resize" not in source, f"{module.__name__} gained a resize path"
        # And no rebalance threshold, which is what would drive one.
        assert "rebalance" not in source

    events = {
        line.split('event="')[1].split('"')[0]
        for line in inspect.getsource(papertrade).splitlines()
        if 'event="' in line
    }
    assert events == {"open", "close"}


def test_the_entry_evidence_is_not_overwritten_by_the_exit_reason():
    """M135: why it was opened and why it was closed sharing one field.

    The signal's reason names the environment the trade was struck in --
    "trend_short|high_volatility @risk_off" -- and the close reason says
    "time stop". Both were briefly called ``reason``, which would have
    replaced the entry evidence with the exit note on every closed trade
    and quietly emptied the dataset this book exists to build.
    """
    from cfdinvest.advisor import papertrade
    from cfdinvest.livehelper.t212_rules import load_rules

    rules = load_rules("config/t212_rules.toml")
    opened = papertrade.open_trade(
        "AAPL", "short", 200.0, "2026-08-26T12:00:00Z", rules=rules, fx_rate=0.7874,
        state={
            "reason": "trend_short|high_volatility @risk_off",
            "confidence": 0.71, "regime": "risk_off", "atr_pct": 3.1,
            "volume_ratio": 1.8, "sentiment": -0.4, "sentiment_n": 31,
        },
    )
    assert opened.signal_reason == "trend_short|high_volatility @risk_off"
    assert opened.reason == ""

    closed = papertrade.value_trade(
        opened.as_row(), 195.0, "2026-08-27T12:00:00Z", reason="time stop"
    )
    assert closed.reason == "time stop"
    # The evidence the decision was made on survives the exit.
    assert closed.signal_reason == "trend_short|high_volatility @risk_off"
    assert "high_volatility" in closed.signal_reason
    # M136: and so does the rest of it. The outcome is only useful next to
    # the state it was predicted from, and a close that dropped the state
    # would leave every question needing a join back to the open row --
    # the sort of step that quietly discards whatever fails to match.
    assert closed.confidence == pytest.approx(0.71)
    assert closed.regime == "risk_off"
    assert closed.atr_pct == pytest.approx(3.1)
    assert closed.volume_ratio == pytest.approx(1.8)
    assert closed.sentiment == pytest.approx(-0.4)
    assert closed.sentiment_n == 31


def test_the_alert_describes_what_the_book_recorded_not_a_separate_suggestion():
    """M167: an email about a decision the book never made.

    The point of these emails is that a selection gets placed by hand on the
    virtual account and compared against the simulated fills. Built from the
    advisor's own suggestion logic they would describe a different decision
    -- different threshold, different sizing, different moment -- and the
    comparison would be between two things that were never the same.
    """
    import inspect

    from cfdinvest.schedule import app, paper_alert

    source = inspect.getsource(app._command_paper_book)
    assert "papertrade.load_book" in source, (
        "the alert is not built from the recorded book"
    )
    assert "suggested_trades" not in source, (
        "the alert is built from the advisor's suggestions, not the book"
    )
    # And it must read back what persisted, not what the pass returned.
    assert "before" in source and "[before:]" in source

    built = inspect.getsource(paper_alert.build_paper_alert)
    assert "advisor" not in built


def test_nothing_is_emailed_when_the_book_did_nothing():
    """Most passes open and close nothing.

    An email every five minutes saying so is how the ones that matter end up
    filtered into a folder nobody opens.
    """
    from cfdinvest.schedule.notify import SmtpConfig
    from cfdinvest.schedule.paper_alert import build_paper_alert

    config = SmtpConfig(sender="a@b.c", recipients=("d@e.f",))
    assert build_paper_alert([], config) is None
    assert build_paper_alert(None, config) is None
    # A pass that only marked positions is also silence.
    assert build_paper_alert([{"event": "mark", "ticker": "AAPL"}], config) is None


def test_the_alert_carries_a_plain_text_body_as_well_as_html():
    """M168: meaning that lives only in the markup.

    The plain part is the real message -- it is what a screen reader, a
    text-only client and a phone's notification preview all show. HTML is a
    nicer rendering of it, never a replacement.
    """
    from cfdinvest.schedule.notify import SmtpConfig
    from cfdinvest.schedule.paper_alert import build_paper_alert

    events = [{
        "event": "open", "cohort": "c50", "ticker": "NKE", "direction": "short",
        "quantity": 25.93, "price": 38.642, "notional_gbp": 1000.0,
        "margin_gbp": 200.0, "confidence": 0.513,
    }]
    message = build_paper_alert(events, SmtpConfig(sender="a@b.c", recipients=("d@e.f",)))

    plain = message.get_body(("plain",)).get_content()
    rich = message.get_body(("html",)).get_content()

    # Everything needed to place the trade is in the text, not just the HTML.
    for needed in ("NKE", "SHORT", "25.9", "38.64"):
        assert needed in plain, f"{needed!r} appears only in the HTML"
    assert "simulated" in plain.lower(), "the text does not say it is simulated"
    assert "<" not in plain, "markup leaked into the plain part"

    # And the HTML must be inline-styled: every mail client strips <style>.
    assert "<style" not in rich
    assert "style=" in rich


def test_the_alert_says_it_is_simulated_in_both_parts():
    """It describes trades nobody placed, on an account that is not real.

    This system has never placed an order and has no broker connection. An
    email that reads like a broker confirmation is the one way that could
    stop being obvious.
    """
    from cfdinvest.schedule.notify import SmtpConfig
    from cfdinvest.schedule.paper_alert import build_paper_alert

    events = [{
        "event": "close", "cohort": "c70", "ticker": "AAPL", "direction": "long",
        "net_pnl_gbp": -4.21, "days_held": 0.29, "reason": "time stop",
    }]
    message = build_paper_alert(events, SmtpConfig(sender="a@b.c", recipients=("d@e.f",)))

    for part in ("plain", "html"):
        body = message.get_body((part,)).get_content()
        assert "simulated" in body.lower(), f"the {part} part does not say so"


def test_only_signals_and_errors_are_emailed_by_default():
    """M169: twenty-six performance emails a session.

    send_daily_summary fired after every pipeline run. That was once a day
    when it was written; since the engine moved to hourly bars the pipeline
    runs every fifteen minutes, so it became a performance email every
    quarter hour of every session -- which is precisely how the two emails
    that matter get filtered into a folder nobody opens.

    The defaults now say what is wanted: the paper signals, because a
    selection gets placed by hand and compared, and errors, because they are
    rare and operational. Nothing else.
    """
    import inspect

    from cfdinvest.schedule import app
    from cfdinvest.schedule.notify import SmtpConfig

    config = SmtpConfig()
    assert config.send_paper_signals is True
    assert config.send_errors is True
    assert config.send_run_summary is False, "a summary after every 15-minute run"
    assert config.send_advisor_suggestions is False, "duplicates the paper signals"

    # And the gates are actually consulted, not merely declared.
    source = inspect.getsource(app)
    for call, gate in (
        ("send_daily_summary(summary, smtp, transport)", "send_run_summary"),
        ("send_advisor_alert(fresh, smtp, transport)", "send_advisor_suggestions"),
    ):
        assert call in source, f"{call} vanished; update this test"
        before = source[: source.index(call)]
        assert gate in before[-400:], f"{call} is not gated by {gate}"


# --------------------------------------------------------------------------- #
# The last look before a trade opens
#
# M227 the recorded signal opened without re-evaluation
# M228 a direction that flipped overnight still traded
# M229 a signal that no longer clears the bar still traded
# M230 a check that could not run treated as a check that passed
# M231 the re-evaluation run once per instrument instead of once per pass
# M232 the refusal recorded as an ordinary skip with no reason
# --------------------------------------------------------------------------- #
#: The real gate, captured at import so the autouse fixture above -- which
#: waves it through for every test that is not about it -- can be undone by
#: the tests that are. Binding the function rather than the module attribute
#: is what makes that possible.
from cfdinvest.advisor.livecheck import holds as _real_holds


def _paper_setup(tmp_path, monkeypatch):
    """A book on disk, an open market, and the *real* last-look gate."""
    from cfdinvest.advisor import livecheck, papertrade, papertrade_run

    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: tmp_path / "book.jsonl"
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    monkeypatch.setattr(livecheck, "holds", _real_holds)
    return papertrade, papertrade_run


def test_a_signal_that_no_longer_holds_is_not_opened(tmp_path, monkeypatch):
    """M228/M229: yesterday's conviction, filled at today's price.

    The signal is computed on the last bar that closed. At a session open
    that is the previous evening, while the fill happens at a price holding
    the whole overnight move -- an earnings release, a downgrade, a sector
    repricing. Measured over 2,125 round trips, requiring the freshly
    computed signal to still agree and still clear the bar refused 201 of
    them; those refused won 36.3% and averaged -0.331% against +0.011% for
    the ones kept (t=+4.63, p<0.0001).
    """
    from cfdinvest.advisor import livecheck

    papertrade, papertrade_run = _paper_setup(tmp_path, monkeypatch)

    monkeypatch.setattr(
        livecheck, "reevaluate",
        lambda tickers, prices, **kw: {
            # Still long and still convinced.
            "KEEP": {"direction": "long", "confidence": 0.90},
            # The overnight move turned it round.
            "FLIP": {"direction": "short", "confidence": 0.90},
            # Same direction, no longer worth acting on.
            "FADE": {"direction": "long", "confidence": 0.10},
        },
    )

    signals = [_signal(t, direction="long", confidence=0.8)
               for t in ("KEEP", "FLIP", "FADE")]
    prices = {t: 100.0 for t in ("KEEP", "FLIP", "FADE")}

    result = papertrade_run.run_paper_pass(
        signals, prices, now="2026-08-27T13:35:00Z", blackout_days=0,
        threshold=0.35,
    )

    assert result["opened"] == 1
    book = papertrade.load_book()
    assert [row["ticker"] for row in book] == ["KEEP"]
    assert result["skipped"].get("direction reversed since the signal") == 1
    assert result["skipped"].get("no longer clears the bar") == 1


def test_a_check_that_could_not_run_is_not_a_check_that_passed(tmp_path, monkeypatch):
    """M230: the gate that opens when it breaks.

    An unreadable lake, a vendor timeout, an exception anywhere in the
    re-evaluation -- all come back as "nothing was checked". Treating that
    as approval would make the gate disappear at exactly the moment
    something is wrong with the data it was meant to be reading.
    """
    from cfdinvest.advisor import livecheck

    papertrade, papertrade_run = _paper_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(livecheck, "reevaluate", lambda tickers, prices, **kw: {})

    result = papertrade_run.run_paper_pass(
        [_signal("AAPL", direction="long", confidence=0.9)], {"AAPL": 100.0},
        now="2026-08-27T13:35:00Z", blackout_days=0, threshold=0.35,
    )

    assert result["opened"] == 0
    assert result["skipped"].get("not re-checked") == 1, (
        "an unchecked signal must not be traded, and the log must say why"
    )


def test_the_re_evaluation_runs_once_for_the_whole_pass(tmp_path, monkeypatch):
    """M231: a pipeline rebuild per instrument.

    The check reads the lake and rebuilds the panels. Run per instrument it
    would cost twenty times what it is worth on a twenty-name universe, on
    every five-minute tick.
    """
    from cfdinvest.advisor import livecheck

    papertrade, papertrade_run = _paper_setup(tmp_path, monkeypatch)

    calls = []

    def record(tickers, prices, **kw):
        calls.append(list(tickers))
        return {t: {"direction": "long", "confidence": 0.9} for t in tickers}

    monkeypatch.setattr(livecheck, "reevaluate", record)

    tickers = [f"T{i:02d}" for i in range(6)]
    papertrade_run.run_paper_pass(
        [_signal(t, direction="long", confidence=0.9) for t in tickers],
        {t: 100.0 for t in tickers},
        now="2026-08-27T13:35:00Z", blackout_days=0, threshold=0.35,
    )

    assert len(calls) == 1, f"re-evaluated {len(calls)} times, once was enough"
    assert sorted(calls[0]) == sorted(tickers)


def test_nothing_is_re_evaluated_when_nothing_would_open(tmp_path, monkeypatch):
    """M227's other half: paying for a check with nothing to check.

    Most passes open nothing. Rebuilding the panels to confirm a decision
    that was never going to be taken is pure cost, every five minutes,
    forever.
    """
    from cfdinvest.advisor import livecheck

    papertrade, papertrade_run = _paper_setup(tmp_path, monkeypatch)

    calls = []
    monkeypatch.setattr(
        livecheck, "reevaluate",
        lambda tickers, prices, **kw: calls.append(1) or {},
    )

    result = papertrade_run.run_paper_pass(
        [_signal("AAPL", direction="long", confidence=0.10)], {"AAPL": 100.0},
        now="2026-08-27T13:35:00Z", blackout_days=0, threshold=0.35,
    )

    assert result["opened"] == 0
    assert calls == [], "re-evaluated with no candidate to re-evaluate"


def test_an_absent_instrument_is_refused_rather_than_assumed(tmp_path, monkeypatch):
    """M230, one instrument at a time.

    A re-evaluation that returned readings for two of three names has not
    said the third is fine. It has said nothing about the third.
    """
    from cfdinvest.advisor import livecheck

    papertrade, papertrade_run = _paper_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        livecheck, "reevaluate",
        lambda tickers, prices, **kw: {"HERE": {"direction": "long", "confidence": 0.9}},
    )

    result = papertrade_run.run_paper_pass(
        [_signal("HERE", direction="long", confidence=0.9),
         _signal("GONE", direction="long", confidence=0.9)],
        {"HERE": 100.0, "GONE": 100.0},
        now="2026-08-27T13:35:00Z", blackout_days=0, threshold=0.35,
    )

    assert result["opened"] == 1
    assert result["skipped"].get("not re-checked") == 1


# --------------------------------------------------------------------------- #
# What the re-evaluation itself computes
# --------------------------------------------------------------------------- #
def test_the_provisional_bar_extends_the_lake_and_never_rewrites_it():
    """M233: a stale quote overwriting a bar that really happened.

    A vendor returning yesterday's price is a thing that happens. Appending
    it as though it were now would replace a real observation with a stale
    one and then compute a signal from it.
    """
    import pandas as pd

    from cfdinvest.advisor.livecheck import ProvisionalRepository

    index = pd.date_range("2026-08-26 17:30", periods=2, freq="h", tz="UTC")
    stored = pd.DataFrame({
        "timestamp": list(index) * 1,
        "ticker": ["AAPL", "AAPL"],
        "open": [100.0, 101.0], "high": [100.0, 101.0],
        "low": [100.0, 101.0], "close": [100.0, 101.0],
        "volume": [1e6, 1e6],
    })

    class Inner:
        config = None

        def load_market(self, *a, **k):
            return stored

    fresh = ProvisionalRepository(Inner(), {"AAPL": 105.0}, "2026-08-27T13:35:00Z")
    extended = fresh.load_market()
    assert len(extended) == 3
    assert extended["close"].iloc[-1] == 105.0

    stale = ProvisionalRepository(Inner(), {"AAPL": 105.0}, "2026-08-26T12:00:00Z")
    assert len(stale.load_market()) == 2, "a quote older than the lake was appended"


def test_the_last_look_does_not_inherit_the_session_open_rule():
    """M234: a check that echoes the signal it is meant to question.

    ``hold_across_session_open`` carries the previous bar's stance across a
    session's first bar. The provisional bar *is* a session's first bar, so
    with that rule on, the re-evaluation would return the stale signal back
    to the caller and agree with itself every time.
    """
    from pathlib import Path

    source = Path("cfdinvest/advisor/livecheck.py").read_text(encoding="utf-8")
    assert "hold_across_session_open=False" in source
