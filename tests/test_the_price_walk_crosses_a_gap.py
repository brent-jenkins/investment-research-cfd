"""The price walk crosses the overnight gap instead of stopping at it.

Measured against Capital on 2026-09-27: minute bars asked for from 20:00 on
2026-08-07 come back as the 60 bars to 20:59 and no further. Asked again
FROM 20:59:00 the server returns that single bar; asked from 20:59:01 it
returns the next session, starting 08:00 on 2026-08-10.

The walk advanced its cursor TO the newest bar, got the one bar back, saw
nothing new and stopped -- so every nightly minute ingest resumed from its
tail, reached the first gap, added nothing and reported success. Most of
the 66 capital_1m series stopped on 2026-04-02 and MU on 2026-08-07 while
the job ran every night.

The fake server here reproduces that behaviour: a page is the bars at or
after ``from`` up to the end of that session, so only a walk that steps past
its newest bar ever sees the second session.

Mutations checked:

  M1850 the cursor set to the newest bar again (the defect restored)
  M1851 the no-progress break removed, so a server repeating itself spins
"""
from __future__ import annotations

import urllib.parse

import pandas as pd

from cfdinvest.broker.capital import prices as capital_prices
from tests.test_capital_prices import Session, _config, bar


def _two_sessions():
    """Four bars a session, two sessions, a night between them."""
    first = [f"2026-08-07T20:{m:02d}:00" for m in (56, 57, 58, 59)]
    second = [f"2026-08-10T08:{m:02d}:00" for m in (0, 1, 2, 3)]
    return first, second


def capital_like(sessions):
    """Serve bars at or after ``from``, stopping at the end of that session."""
    calls = {"froms": []}

    def opener(url, headers=None, **kwargs):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start = pd.Timestamp(query["from"][0], tz="UTC")
        calls["froms"].append(start)
        for session in sessions:
            later = [s for s in session if pd.Timestamp(s, tz="UTC") >= start]
            if later:
                return {"prices": [bar(s, 100.0, 100.1) for s in later]}, {}
        return {"prices": []}, {}

    opener.calls = calls
    return opener


def test_the_walk_reaches_the_session_after_a_gap():
    """M1850: the walk that stopped at the first overnight gap."""
    first, second = _two_sessions()
    opener = capital_like([first, second])

    rows = capital_prices.fetch_prices(
        "MU", Session(), _config(), resolution="1m", start="2026-08-07T20:56:00",
        opener=opener, sleep=lambda _s: None, max_calls=10,
    )

    stamps = {r["timestamp"] for r in rows}
    assert pd.Timestamp(second[-1], tz="UTC") in stamps, (
        "the walk stopped at the overnight gap: it asked again from the last "
        "bar it had, got that bar back, and read 'nothing new' as the end of "
        f"history. Asked from: {[str(f) for f in opener.calls['froms']]}"
    )
    assert len(stamps) == len(first) + len(second)


def test_the_walk_still_ends_when_history_does():
    """M1851: stepping past the newest bar must not make an endless walk."""
    first, second = _two_sessions()
    opener = capital_like([first, second])

    capital_prices.fetch_prices(
        "MU", Session(), _config(), resolution="1m", start="2026-08-07T20:56:00",
        opener=opener, sleep=lambda _s: None, max_calls=50,
    )
    assert len(opener.calls["froms"]) <= 4, (
        f"the walk made {len(opener.calls['froms'])} calls over two sessions"
    )
