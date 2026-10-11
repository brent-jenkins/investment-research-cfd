"""Market details in batches of fifty, so one extra name cannot unprice all.

On 2026-10-09 the dealing list was 51 names; Capital answered HTTP 400 to
the whole request and every market, held or not, came back unpriced.

Mutations checked:

  M2020 one request for every epic, whatever the count
  M2021 a failed batch taking the successful ones down with it
"""
from __future__ import annotations

import urllib.error
import urllib.parse

from cfdinvest.broker.capital import client


class _Session:
    ok, host, error = True, "https://capital", ""

    def headers(self, _key):
        return {}


def _opener(fail_over=50, refuse=()):
    def read(url, headers=None):
        epics = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["epics"][0].split(",")
        if len(epics) > fail_over or set(epics) & set(refuse):
            raise urllib.error.HTTPError(url, 400, "bad", {}, None)
        return {"marketDetails": [
            {"instrument": {"epic": e}, "snapshot": {"bid": 100.0, "offer": 100.1}} for e in epics
        ]}, None
    return read


def test_more_than_fifty_names_are_all_priced():
    """M2020."""
    names = [f"N{i}" for i in range(51)]
    out = client.fetch_market_details(names, _Session(), client.CapitalConfig(), opener=_opener())
    assert [m.epic for m in out] == names
    assert not [m for m in out if m.error], [m.error for m in out if m.error][:3]


def test_a_failed_batch_does_not_unprice_the_others():
    """M2021."""
    names = [f"N{i}" for i in range(60)]
    out = client.fetch_market_details(names, _Session(), client.CapitalConfig(),
                                      opener=_opener(refuse=("N55",)))
    priced = {m.epic for m in out if not m.error}
    assert priced == set(names[:50])
