"""Form 4 insider transactions: the codes, the dates, and the asymmetry.

Insider *buying* measured +1.04% market-adjusted over the following session
(n=105, t=3.42, p=0.0009). Insider *selling* measured nothing at all. Both
facts have to survive in the code, because the obvious implementation --
net the shares and take the sign -- destroys the first by averaging it with
the second.

M416 compensation counted as a decision, so payroll drowns the signal
M417 keyed on the transaction date rather than the filing date, which is
     two business days of lookahead and makes the study look far better
M418 buys and sells netted into one number, diluting the half that works
M419 a re-fetch double-counting transactions already stored
M420 two genuinely identical trades on one day collapsed into one
M421 a malformed filing stopping the pass
"""

from __future__ import annotations

import json

import pytest

from cfdinvest.ingestion.insiders import (
    MEANINGFUL_CODES,
    _transactions,
    insider_flow,
    load_insiders,
    store_insiders,
)


def form4(code="P", shares="1000", price="250.5", date="2026-08-20"):
    return f"""<?xml version="1.0"?>
    <ownershipDocument>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <securityTitle><value>Common Stock</value></securityTitle>
          <transactionDate><value>{date}</value></transactionDate>
          <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
          <transactionAmounts>
            <transactionShares><value>{shares}</value></transactionShares>
            <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
          </transactionAmounts>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""


def test_only_open_market_decisions_are_counted():
    """M416: payroll read as conviction.

    A grant, an option exercise and tax withholding are things that happen
    *to* an insider. Only P and S are decisions to take a position at the
    prevailing price, and pooling them is how this analysis is usually got
    wrong -- there were 20,508 sales against 228 purchases in the sample,
    and vastly more compensation events than either.
    """
    assert MEANINGFUL_CODES == {"P", "S"}
    for code in ("A", "M", "F", "G", "C", "D"):
        assert _transactions(form4(code=code)) == [], (
            f"code {code} is compensation machinery and was counted"
        )
    assert len(_transactions(form4(code="P"))) == 1
    assert len(_transactions(form4(code="S"))) == 1


def test_a_transaction_is_parsed_with_its_numbers_intact():
    rows = _transactions(form4(code="P", shares="1500", price="99.25"))
    assert rows == [{
        "code": "P", "shares": 1500.0, "price": 99.25,
        "transacted": "2026-08-20",
    }]


def test_a_malformed_filing_yields_nothing_rather_than_raising():
    """M421: one bad document stopping a daily pass."""
    assert _transactions("not xml at all") == []
    assert _transactions("<nonDerivativeTransaction></nonDerivativeTransaction>") == []
    broken = form4(shares="not-a-number")
    assert _transactions(broken) == []


def test_buys_and_sells_are_reported_separately(tmp_path):
    """M418: the netting that destroys the finding.

    Buying predicted +1.04% and selling predicted nothing. Netting them
    into a single signed number averages a real effect with a null one, so
    both are carried and a caller can use the half that works.
    """
    rows = {"AAA": [
        {"filed": "2026-08-20", "code": "P", "shares": 1000.0, "price": 100.0},
        {"filed": "2026-08-21", "code": "S", "shares": 4000.0, "price": 100.0},
    ]}
    flow = insider_flow(rows, now="2026-08-25")["AAA"]

    assert flow["buys"] == 1 and flow["sells"] == 1
    assert flow["value_bought"] == pytest.approx(100_000.0)
    assert flow["value_sold"] == pytest.approx(400_000.0)
    # The net exists but must not be the only thing available: netted, this
    # instrument looks like heavy selling and the purchase disappears.
    assert flow["net_value"] == pytest.approx(-300_000.0)
    assert flow["value_bought"] > 0, "the purchase was absorbed into the net"


def test_the_filing_date_gates_visibility_not_the_transaction_date():
    """M417: two business days of lookahead.

    Insiders have two business days to report. A signal keyed on the
    transaction date trades on information that was not public, which is
    exactly what makes a naive version of this study look profitable.
    """
    rows = {"AAA": [{
        "filed": "2026-08-24", "code": "P", "shares": 1000.0,
        "price": 100.0, "transacted": "2026-08-20",
    }]}
    # After the trade but before it was filed: not yet knowable.
    assert insider_flow(rows, now="2026-08-22") == {}
    # On the filing date: knowable.
    assert "AAA" in insider_flow(rows, now="2026-08-24")


def test_nothing_after_now_is_ever_included():
    rows = {"AAA": [{"filed": "2026-09-10", "code": "P", "shares": 1.0, "price": 1.0}]}
    assert insider_flow(rows, now="2026-08-25") == {}


def test_the_lookback_window_is_respected():
    rows = {"AAA": [
        {"filed": "2026-01-05", "code": "P", "shares": 1000.0, "price": 100.0},
        {"filed": "2026-08-20", "code": "P", "shares": 500.0, "price": 100.0},
    ]}
    recent = insider_flow(rows, now="2026-08-25", lookback_days=90)["AAA"]
    assert recent["shares_bought"] == pytest.approx(500.0), (
        "a purchase from seven months ago is still being counted as current"
    )
    everything = insider_flow(rows, now="2026-08-25", lookback_days=3650)["AAA"]
    assert everything["shares_bought"] == pytest.approx(1500.0)


def test_storing_twice_does_not_double_count(tmp_path):
    """M419: a re-fetch inflating every position."""
    class Paths:
        data_root = str(tmp_path)

    class Config:
        paths = Paths()

    rows = {"AAA": [
        {"filed": "2026-08-20", "code": "P", "shares": 1000.0, "price": 100.0},
    ]}
    first = store_insiders(rows, Config())
    second = store_insiders(rows, Config())
    assert first == second == 1, "re-storing the same filing counted it twice"

    stored = load_insiders(Config())
    assert len(stored["AAA"]) == 1


def test_two_identical_trades_on_one_day_both_survive(tmp_path):
    """M420: real activity discarded as a duplicate.

    An insider genuinely can make two identical trades on one day. Keying
    deduplication on the ticker and date alone would drop the second and
    understate the position.
    """
    class Paths:
        data_root = str(tmp_path)

    class Config:
        paths = Paths()

    same_day = {"AAA": [
        {"filed": "2026-08-20", "code": "P", "shares": 1000.0, "price": 100.0},
        {"filed": "2026-08-20", "code": "P", "shares": 2000.0, "price": 101.0},
    ]}
    assert store_insiders(same_day, Config()) == 2
    flow = insider_flow(load_insiders(Config()), now="2026-08-25")["AAA"]
    assert flow["shares_bought"] == pytest.approx(3000.0)


def test_an_absent_store_is_empty_rather_than_an_error(tmp_path):
    class Paths:
        data_root = str(tmp_path / "nothing-here")

    class Config:
        paths = Paths()

    assert load_insiders(Config()) == {}
    assert insider_flow({}, now="2026-08-25") == {}


def test_the_daily_refresh_actually_fetches_them():
    """A collector nothing calls collects nothing.

    The signal fires roughly thirty times a year across the whole pool, so
    a wiring mistake here costs a year of sample before anyone notices --
    there is no volume of activity to make the absence obvious.
    """
    import inspect

    from cfdinvest.schedule import runner

    source = inspect.getsource(runner._refresh_events)
    assert "insiders_module.fetch_insiders(" in source, (
        "the daily refresh does not fetch insider transactions"
    )
    assert "insiders_module.store_insiders(" in source
    assert "since=latest" in source, (
        "the fetch is not incremental, so every pass re-reads the archive"
    )


def test_the_refresh_covers_the_candidates_not_only_the_traded_list():
    """The screen ranks 49 names; a signal on 8 of them cannot inform it."""
    import inspect

    from cfdinvest.schedule import runner

    source = inspect.getsource(runner._refresh)
    assert "_with_candidates(tickers, config)" in source
    assert source.count("_refresh_events(") == 1, (
        "two call sites would fetch under two different universes"
    )
