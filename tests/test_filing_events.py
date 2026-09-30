"""The filing backtest: pre-registered, so its parts must mean what they say.

Mutations checked:

  M1920 the press release not preferred over the 8-K's own document
  M1921 a filing accepted AFTER 10:00 counted for that session
  M1922 a filing older than 24 hours counted
  M1923 the "already priced" cut ignoring which way the pre-market went
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.research import filing_events as fe
from cfdinvest.signals.news import events_from_labels

TEN = pd.Timestamp("2025-09-10 14:00", tz="UTC")  # 10:00 New York in summer


INDEX = """<table>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/x/nvda-20260826.htm">nvda-20260826.htm</a> &nbsp;&nbsp;iXBRL</td><td>8-K</td></tr>
<tr><td>2</td><td>EX-99.1</td><td><a href="/x/q2fy27pr.htm">q2fy27pr.htm</a></td><td>EX-99.1</td></tr>
<tr><td>3</td><td>EX-99.2</td><td><a href="/x/q2fy27cfo.htm">q2fy27cfo.htm</a></td><td>EX-99.2</td></tr>
</table>"""


def test_the_press_release_is_found_by_its_type_not_its_name():
    """M1920: NVIDIA's results release is q2fy27pr.htm -- no "99" in the name."""
    types = fe.exhibits(INDEX)
    assert types["EX-99.1"] == "q2fy27pr.htm"
    assert fe.choose_document(types, "nvda-20260826.htm") == "q2fy27pr.htm"
    assert fe.choose_document({"8-K": "x.htm"}, "x.htm") == "x.htm"


def test_an_8k_is_read_from_its_first_item_not_its_cover_page():
    cover = "FORM 8-K CURRENT REPORT Commission File Number ... Item 5.02 Departure of Directors"
    assert fe.body(cover, is_exhibit=False).startswith("Item 5.02")
    assert fe.body("Revenue rose", is_exhibit=True) == "Revenue rose"


def test_html_becomes_readable_text():
    text = fe.plain_text("<html><style>p{}</style><p>Revenue&nbsp;rose</p><p>12%</p></html>")
    assert "Revenue rose" in text and "12%" in text and "p{}" not in text


def _sessions():
    return pd.DataFrame([{"ticker": "AAA", "day": TEN.normalize(), "ten": TEN,
                          "move": 0.01, "pre": 0.0, "cost": 0.001, "typical": 0.01}])


def _events(*stamps):
    return events_from_labels(
        [{"key": str(s), "prompt_version": "v", "ticker": "AAA", "about": "company",
          "event": "guidance_up", "impact": "good", "first_seen": s.isoformat()} for s in stamps],
        prompt_version="v",
    )


def test_only_filings_accepted_in_the_24_hours_before_ten_count():
    """M1921, M1922."""
    inside = _events(TEN - pd.Timedelta(hours=17))          # 21:00 UTC the day before
    after = _events(TEN + pd.Timedelta(minutes=5))
    stale = _events(TEN - pd.Timedelta(hours=25))
    assert fe.filing_sides(inside, _sessions())["side"].iloc[0] == 1
    assert fe.filing_sides(after, _sessions())["side"].iloc[0] == 0, "counted a filing the account could not have seen"
    assert fe.filing_sides(stale, _sessions())["side"].iloc[0] == 0, "counted a filing older than the window"


def test_already_priced_means_the_premarket_went_the_filings_way():
    """M1923: a large pre-market move AGAINST the filing is not "priced in"."""
    rows = []
    for d, pre in enumerate((0.02, -0.02, 0.001)):
        day = TEN + pd.Timedelta(days=d)
        rows.append({"ticker": "AAA", "day": day.normalize(), "ten": day, "move": 0.005,
                     "pre": pre, "cost": 0.0, "typical": 0.01, "side": 1})
    result = fe.study(pd.DataFrame(rows))
    assert result["already_priced"]["name_days"] == 1
    assert result["not_yet_priced"]["name_days"] == 2


def test_the_window_starts_after_the_models_training_data():
    """Hindsight: the model must not have read what came after a filing."""
    assert fe.START >= "2025-08-01"
    assert np.isfinite(fe.MAX_CHARS)
