"""The volatility-band test bands by the screen's reading, and judges fairly.

Pre-registered in CLAUDE.md on 2026-11-03. Mutations checked:

  M2140 bands cut by score rather than by volatility
  M2141 a name in two bands (overlapping cuts)
  M2142 the sector cap not applied within a band
  M2143 an unranked candidate (no volatility) placed in a band
  M2144 the pass bar left at t 2 rather than the registered 2.4
  M2145 a pass allowed with one half negative
"""
from __future__ import annotations

from cfdinvest.research import vol_bands as vb
from cfdinvest.universe.screen import Candidate


def _pool(n=60):
    sectors = ["tech", "health", "energy", "fin", "util", "staples"]
    # best score is the MOST volatile, so a best-score-first cut and an
    # ascending-volatility cut put opposite names in "low"
    return [Candidate(f"N{i:02d}", volatility=0.1 + i / 100, score=float(i),
                      sector=sectors[i % len(sectors)]) for i in range(n)]


def test_bands_follow_volatility_not_score():
    """M2140."""
    bands = vb.bands_on(_pool(), target=10, cap=None)
    vol = {c.ticker: c.volatility for c in _pool()}
    assert max(vol[t] for t in bands["low"]) < min(vol[t] for t in bands["mid"])
    assert max(vol[t] for t in bands["mid"]) < min(vol[t] for t in bands["high"])


def test_no_name_is_in_two_bands():
    """M2141."""
    bands = vb.bands_on(_pool(), target=20, cap=None)
    names = [t for b in bands.values() for t in b]
    assert len(names) == len(set(names)) == 60


def test_the_sector_cap_binds_inside_a_band():
    """M2142."""
    sector = {c.ticker: c.sector for c in _pool()}
    bands = vb.bands_on(_pool(), target=15, cap=2)
    for names in bands.values():
        counts = {}
        for t in names:
            counts[sector[t]] = counts.get(sector[t], 0) + 1
        assert max(counts.values()) <= 2
        assert len(names) == 12  # six sectors x 2: the fill is short, not padded


def test_an_unmeasured_name_is_never_banded():
    """M2143."""
    pool = _pool() + [Candidate("ZZZ", unranked="no closes")]
    assert "ZZZ" not in {t for b in vb.bands_on(pool, target=30, cap=None).values() for t in b}


def test_the_pass_mark_is_the_registered_one():
    """M2144, M2145."""
    ok = {"net": 0.001, "net_t": 2.5, "h1": 0.001, "h2": 0.001}
    assert vb.verdict(ok)
    assert not vb.verdict({**ok, "net_t": 2.2})
    assert not vb.verdict({**ok, "h2": -0.0001})
    assert not vb.verdict({**ok, "net": -0.001})
