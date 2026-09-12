"""The macro regime can be measured on the series' own calendar.

THE UNITS BUG
-------------
The macro lookbacks are denominated in bars, and only one of them is scaled
to the bar size. `_macro_lookback` turns a year into `periods_per_year` --
1,764 on the hourly grid -- while `macro_momentum_lookback` (63),
`labor_lookback` (63) and `labor_smoothing` (21) stay as written. Those are
the DAILY conventions: 252 a year, 63 a quarter, 21 a month. So on the
hourly panel the quarter is nine trading days and the month is three, on
FRED series that change twelve times a year.

Same class as `capital_1h` not being a pandas frequency: a constant carrying
an assumption about its units, applied to data that no longer satisfies it.

It is also why a six-month analysis window failed. The classifier needs
1,827 bars before it says anything but `unknown`, so a six-month run
reported the regime unknown on every bar and dropped positions the full-span
run held -- which was never a statement about how much history a four-hour
trade needs.

THE TRAP THE FIRST ATTEMPT FELL INTO
------------------------------------
Classifying on the monthly calendar and then reindexing the labels onto the
hourly one matches almost no rows, so every bar became `unknown`. The
measurement showed the six-month and full-span runs agreeing on 5 of 5 --
because BOTH had gone blind. Agreement because neither side can see anything
is the worst kind of green, and it would have shipped as a fix.

Mutations checked:

  M1700 the macro classified on the bar calendar while the flag is on
  M1701 labels reindexed without carrying, so every bar is unknown
  M1702 carry applied to the bar-calendar components, masking real gaps
  M1703 a label carried BACKWARDS before its first observation
  M1704 macro_native reindexed onto the price calendar, losing its own dates
"""
from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.regimes import frame as regime_frame
from cfdinvest.regimes.labels import UNKNOWN


def _calendar(hours: int = 6):
    return pd.DatetimeIndex(
        pd.date_range("2026-09-01 13:00", periods=hours, freq="h", tz="UTC")
    )


def test_a_coarse_label_is_carried_across_the_bars_it_governs() -> None:
    """M1701: a monthly label reindexed onto hourly bars matches nothing."""
    calendar = _calendar(6)
    monthly = pd.Series(
        ["expansion"],
        index=pd.DatetimeIndex([pd.Timestamp("2026-09-01 13:00", tz="UTC")]),
    )

    carried = regime_frame._conform(
        monthly, calendar, ("expansion", UNKNOWN), "macro_regime", carry=True
    )

    assert list(carried) == ["expansion"] * 6, (
        "the label published most recently at or before each bar governs it"
    )


def test_without_carry_a_gap_is_named_rather_than_filled() -> None:
    """M1702: on the bar calendar a gap is a real absence.

    Carrying the previous label over it reports a stale reading as a current
    one, which breaks "absence is named, never inferred" in the module that
    defines the word.
    """
    calendar = _calendar(3)
    sparse = pd.Series(["expansion"], index=calendar[:1])

    plain = regime_frame._conform(
        sparse, calendar, ("expansion", UNKNOWN), "macro_regime"
    )

    assert list(plain) == ["expansion", UNKNOWN, UNKNOWN]


def test_a_label_is_never_carried_backwards() -> None:
    """M1703: bars before the first observation are unknown, not the first
    label projected into the past. That is hindsight."""
    calendar = _calendar(4)
    later = pd.Series(["contraction"], index=calendar[2:3])

    carried = regime_frame._conform(
        later, calendar, ("contraction", UNKNOWN), "macro_regime", carry=True
    )

    assert list(carried)[:2] == [UNKNOWN, UNKNOWN]
    assert list(carried)[2:] == ["contraction", "contraction"]


def test_an_empty_component_is_unknown_throughout() -> None:
    calendar = _calendar(3)
    for carry in (False, True):
        conformed = regime_frame._conform(
            pd.Series(dtype="object"), calendar, ("x", UNKNOWN),
            "macro_regime", carry=carry,
        )
        assert list(conformed) == [UNKNOWN] * 3


def test_the_switch_defaults_to_the_shipped_behaviour() -> None:
    """A correctness fix must not move what the account trades by itself.

    Same treatment as `time_stop_uses_completed_bar`: the correction exists,
    is tested, and is not flipped as a side effect of a bug fix.
    """
    assert regime_frame.RegimeConfig().macro_on_own_calendar is False


def test_the_month_denominated_spans_are_a_year_and_a_quarter() -> None:
    """M1700: the point is that 12 and 3 mean what 252 and 63 meant."""
    assert regime_frame.MACRO_MONTHS_PER_YEAR == 12
    assert regime_frame.MACRO_MONTHS_PER_QUARTER == 3
    assert regime_frame.MACRO_MONTHS_PER_QUARTER * 4 == regime_frame.MACRO_MONTHS_PER_YEAR


def _long(series: str, values, stamps, published=None):
    """A stacked macro frame the way the repository returns one."""
    from cfdinvest.schema import MACRO_VALUE, PUBLICATION_DATE, SERIES_ID, TIMESTAMP

    data = {SERIES_ID: [series] * len(values), MACRO_VALUE: list(values)}
    if published is not None:
        data[PUBLICATION_DATE] = list(published)
    return pd.DataFrame(data, index=pd.Index(stamps, name=TIMESTAMP))


def test_the_monthly_panel_is_monthly_and_not_the_union_of_every_series() -> None:
    """M1704: "its own calendar" is not the union of all the series' dates.

    The first version pivoted on observation dates, which is that union --
    and DFF, DGS10 and VIXCLS are DAILY, so the index came out daily with
    13,403 rows. INDPRO's 439 monthly values sat scattered among them and a
    twelve-row shift moved back twelve DAYS onto a NaN, so the classifier
    returned unknown everywhere and the fix appeared to do nothing.
    """
    from cfdinvest.preprocessing.frame import PreprocessConfig, _build_macro_monthly

    monthly_stamps = pd.date_range("2026-01-31", periods=3, freq="ME", tz="UTC")
    daily_stamps = pd.date_range("2026-01-01", periods=90, freq="D", tz="UTC")

    long = pd.concat([
        _long("CPIAUCSL", [100.0, 101.0, 102.0], monthly_stamps,
              published=monthly_stamps),
        _long("DFF", [5.0] * 90, daily_stamps, published=daily_stamps),
    ])

    monthly = _build_macro_monthly(long, PreprocessConfig())

    assert len(monthly) <= 4, (
        f"a daily series dragged the grid to {len(monthly)} rows; a year must "
        "be twelve rows, not twelve days"
    )
    assert "CPIAUCSL" in monthly.columns and "DFF" in monthly.columns


def test_the_monthly_panel_is_indexed_by_publication_not_observation() -> None:
    """A figure for August is not knowable on 1 September.

    Indexing by publication is what makes carrying it forward honest, and is
    the same question `publication_aware_ffill` answers for the bar panel.
    Without it this is a quiet lookahead where nobody would check.
    """
    from cfdinvest.preprocessing.frame import PreprocessConfig, _build_macro_monthly

    observed = pd.DatetimeIndex([pd.Timestamp("2026-01-31", tz="UTC")])
    published = pd.DatetimeIndex([pd.Timestamp("2026-03-15", tz="UTC")])

    monthly = _build_macro_monthly(
        _long("CPIAUCSL", [100.0], observed, published=published),
        PreprocessConfig(),
    )

    assert len(monthly), "the observation should appear once published"
    # It must land in the month it was PUBLISHED, not the month it describes.
    assert monthly.index[0] >= pd.Timestamp("2026-03-01", tz="UTC"), (
        "indexed by observation date, which back-dates a figure to before it "
        "was knowable"
    )


def test_the_monthly_panel_is_empty_when_there_is_no_macro() -> None:
    from cfdinvest.preprocessing.frame import PreprocessConfig, _build_macro_monthly

    assert _build_macro_monthly(None, PreprocessConfig()).empty
    assert _build_macro_monthly(pd.DataFrame(), PreprocessConfig()).empty
