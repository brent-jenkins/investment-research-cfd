"""A refusal records how far the reading fell, not only that it fell.

`livecheck.holds` computed the live confidence, compared it against the bar,
and returned `(False, "no longer clears the bar")` -- discarding the number
it had just judged on. So the record said THAT a name fell below and never
HOW FAR, and those two outcomes look identical in the store while meaning
opposite things:

    clustered just under the bar   marginality. A gate at roughly the 90th
                                   percentile of the strength distribution
                                   admits only extreme-tail readings, and an
                                   extreme-tail reading is one a small
                                   adverse move knocks under. Expected.

    collapsed toward a third       the 2026-09-10 failure returning, where
                                   the provisional bar was motionless, every
                                   range-scaled input read it as dead, and
                                   NOT ONE of 46 names could reach 0.75
                                   against a highest live reading of 0.656.

Both produce "no longer clears the bar" and a high refusal rate. Only one is
a defect, and until 2026-09-19 nothing in the record could tell them apart.

WHY THE BAR IS RECORDED TOO, and this is the part with a scar behind it.
A reading without its threshold cannot be read later. `entry_confidence`
moved from 0.35 to 0.75 on 2026-09-04, and on 2026-09-19 every refusal
figure taken before that had to be retracted -- 18.7% modelled and 20.7%
measured, quoted four times in one day against a 52.8% measured on the new
gate, when this project's own file already forbade exactly that comparison.
A row carrying its own bar cannot be misread that way, whatever the gate
does afterwards.

NONE IS NOT ZERO. A reading that is absent, unparseable, or never consulted
records `None`. Zero is a confident statement that the signal died, and
`refusal_summary` would fold it into the shortfall as a full-bar collapse --
manufacturing the very failure the field exists to detect.

Mutations checked:

  M1770 `holds` returning only the verdict again, so nothing reaches the row
  M1771 the reading dropped from the refusal Intent.

        **SURVIVED first time.** Every other test here builds an `Intent`
        directly, so the bridge's wiring was never exercised -- the gap
        that made M1754 need its own check. Now tested through the real
        `plan_orders`, not by reading the source: a source assertion was
        not good enough twice today, M1762 having passed against the call
        replaced by `true # market-open`.
  M1772 the bar not recorded, so the shortfall cannot be computed later
  M1773 an absent reading recorded as 0.0 rather than None
  M1774 the shortfall measured against today's config instead of the
        recorded bar
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.advisor.livecheck import holds
from cfdinvest.broker.capital.bridge import Intent
from cfdinvest.persist import orders as orders_store


class _Report:
    halted = False
    halt_reason = ""
    balance = 5000.0

    def __init__(self, intents):
        self.intents = tuple(intents)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(orders_store, "_path", lambda config=None, account=None: path)
    return path


def refusal(ticker, live, bar=0.75, engine=0.80):
    return Intent(
        ticker=ticker, epic=ticker, direction="long",
        held_units=0.0, target_units=0.0, action="refused",
        reason="no longer clears the bar", price=10.0,
        entered_bars_ago=0,
        engine_confidence=engine, live_confidence=live, confidence_bar=bar,
    )


# ------------------------------------------------------------------ #
# The reading comes back with the verdict.
# ------------------------------------------------------------------ #
def test_holds_returns_the_reading_it_judged_on() -> None:
    """M1770: the verdict without the number behind it."""
    still, why, live = holds(
        {"AAA": {"direction": "long", "confidence": 0.71}}, "AAA", "long", 0.75
    )

    assert (still, why) == (False, "no longer clears the bar")
    assert live == pytest.approx(0.71), (
        "the reading the refusal was based on was discarded, so the record "
        "cannot say how far below the bar it fell"
    )


def test_an_approval_also_carries_its_reading() -> None:
    """The passing case must report too, or the distribution is censored.

    Reading only refusals would describe the left tail of the live/engine
    gap and call it the whole distribution -- the shape of the 2026-09-10
    calibration that read 0.000 everywhere because the comparison was a
    no-op by construction.
    """
    still, why, live = holds(
        {"AAA": {"direction": "long", "confidence": 0.80}}, "AAA", "long", 0.75
    )

    assert (still, why) == (True, "")
    assert live == pytest.approx(0.80)


def test_a_missing_reading_is_none_and_never_zero() -> None:
    """M1773: absence recorded as a confident zero.

    Zero says the signal died. `refusal_summary` would read it as a
    full-bar collapse and report exactly the failure this field exists to
    detect, from a row that simply had nothing to say.
    """
    still, why, live = holds({}, "AAA", "long", 0.75)

    assert (still, why) == (False, "not re-checked")
    assert live is None, "an absent reading was recorded as a number"


# ------------------------------------------------------------------ #
# The record keeps all three.
# ------------------------------------------------------------------ #
def test_the_row_carries_the_reading_and_its_bar(store) -> None:
    """M1771, M1772: either number dropped makes the other unreadable."""
    orders_store.record_plan(
        _Report([refusal("AAA", 0.71)]),
        now=pd.Timestamp("2026-09-21 14:00", tz="UTC"),
    )

    row = orders_store.load_orders()[0]
    assert row["live_confidence"] == pytest.approx(0.71)
    assert row["confidence_bar"] == pytest.approx(0.75), (
        "the bar in force was not recorded, so this row can only be read "
        "against whatever the gate happens to be later -- which is the "
        "error retracted on 2026-09-19"
    )
    assert row["engine_confidence"] == pytest.approx(0.80)


def test_the_shortfall_is_measured_against_the_recorded_bar(store) -> None:
    """M1774: measured against today's configuration instead.

    Two refusals taken at DIFFERENT bars, which is what a gate change
    produces in one store. Read against a single current bar they average
    to nonsense; read against their own, each is correct.
    """
    orders_store.record_plan(
        _Report([refusal("AAA", 0.70, bar=0.75)]),
        now=pd.Timestamp("2026-09-21 14:00", tz="UTC"),
    )
    orders_store.record_plan(
        _Report([refusal("BBB", 0.30, bar=0.35)]),
        now=pd.Timestamp("2026-09-21 15:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-21 18:00", tz="UTC")
    )

    assert summary["shortfall_sample"] == 2
    # 0.05 and 0.05 against their own bars. Against a single 0.75 bar the
    # second would read 0.45 and the median would be 0.25.
    assert summary["shortfall_median"] == pytest.approx(0.05), (
        "the shortfall was not measured against the bar recorded on each "
        "row, so a gate change silently corrupts every earlier refusal"
    )


def test_marginality_and_collapse_are_distinguishable(store) -> None:
    """The whole point, stated as the two readings it has to separate."""
    orders_store.record_plan(
        _Report([refusal("AAA", 0.74), refusal("BBB", 0.73), refusal("CCC", 0.72)]),
        now=pd.Timestamp("2026-09-21 14:00", tz="UTC"),
    )
    marginal = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-21 18:00", tz="UTC")
    )["shortfall_median"]

    assert marginal < 0.05, (
        f"three readings within a hundredth of the bar reported a median "
        f"shortfall of {marginal}, which would not read as marginality"
    )


def test_the_bridge_puts_the_live_reading_on_the_refusal() -> None:
    """M1771: the field exists and `plan_orders` never fills it.

    Tested through the real `plan_orders` rather than by reading the source.
    The first attempt at this mutation SURVIVED, because every other test
    here constructs an `Intent` directly and so never exercises the wiring
    -- the same gap that made M1754 need its own check, and the reason a
    source assertion was not good enough twice today: M1762 passed against
    the call replaced by `true # market-open`.

    A field that is always None is the same as no field, and this project
    has shipped that twice.
    """
    from tests.test_broker_capital_bridge import _recheck, only, plan, signal

    faded = plan(
        [signal("AAPL")], recheck=True, rechecked=_recheck(confidence=0.1),
        entry_confidence=0.75,
    )
    intent = only(faded, "AAPL")

    assert intent.action == "refused"
    assert intent.live_confidence == pytest.approx(0.1), (
        "plan_orders refused on a live reading and did not record it, so "
        "the row cannot say how far below the bar the name fell"
    )
    assert intent.confidence_bar == pytest.approx(0.75), (
        "the bar the refusal was judged against was not recorded"
    )


def test_a_store_with_no_readings_reports_no_shortfall(store) -> None:
    """Absence named. Rows written before this existed carry nothing."""
    orders_store.record_plan(
        _Report([Intent(
            ticker="AAA", epic="AAA", direction="long",
            action="refused", reason="no longer clears the bar", price=10.0,
        )]),
        now=pd.Timestamp("2026-09-21 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-21 18:00", tz="UTC")
    )

    assert summary["shortfall_median"] is None, (
        "a store that cannot say reported a number anyway"
    )
    assert summary["shortfall_sample"] == 0
    assert summary["blocked"] == 1, "the existing counts must still work"
