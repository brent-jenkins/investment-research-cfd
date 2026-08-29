"""Choosing which instruments to trade, mechanically and point-in-time.

The universe was assembled by hand. The stated basis -- liquidity,
volatility and sector spread -- was recorded in a commit message rather than
executed as a rule, so it cannot be re-run, cannot be audited, and cannot be
checked for drift. That was tolerable for a list set once. It is not
tolerable for a list refreshed every month or quarter, because every refresh
would carry the same gap, and unconscious selection bias is precisely the
thing you cannot detect without a rule to compare against.

So this is the rule.

**Returns are not an input, deliberately.** Ranking on recent performance is
a momentum overlay: legitimate, possibly good, and a strategy decision that
needs measuring rather than smuggling in through the universe. Keeping it
out means a rebalance cannot quietly become "hold whatever went up", and a
test asserts no return series reaches the score.

**Point-in-time by construction.** Every metric is computed from bars at or
before ``as_of`` and nothing later. A screen that peeks is worse than no
screen, because it produces an audit trail that looks rigorous.

**The candidate pool is the remaining judgement call, and it is not hidden.**
Selecting mechanically from a hand-drawn pool moves the bias rather than
removing it: a pool of names that are large today is still a pool chosen
with hindsight. What this does buy is that the *selection within* the pool is
reproducible, and that the pool is one visible list somebody can argue with
rather than nineteen decisions nobody wrote down.

**Absence is named.** An instrument without enough history is ``unranked``
with a reason, never scored zero -- a zero would sort it last as though it
had been measured and found wanting.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ScreenConfig",
    "Candidate",
    "ScreenResult",
    "rank_candidates",
    "select_universe",
]


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    """What the screen measures and how much each part counts."""

    #: How many instruments to hold. Deliberately small: a tight universe is
    #: what makes it possible to hold detailed data on each name, watch its
    #: filings and read its news. A wider one is monitored more thinly.
    target: int = 19

    #: Trading days of history required before an instrument is ranked at
    #: all. Below this the metrics are noise wearing a number's clothes.
    min_history: int = 120

    #: The window each metric is measured over.
    lookback: int = 120

    #: Most instruments from any one sector. ``None`` disables the cap, and
    #: it is disabled when no sector map is supplied rather than silently
    #: doing nothing -- the result says which happened.
    #:
    #: **Five since 2026-08-29**, and it has been off and on twice, which is
    #: worth explaining because both measurements were right.
    #:
    #: Removed on 2026-08-28 after measuring as worthless on the
    #: forty-nine-name pool: a rank-matched control ignoring sector entirely
    #: beat it 0.75 to 0.65, because capping a sector at N names also drops
    #: the worst-ranked names in a crowded one and that was the whole of the
    #: effect. True of that pool -- 22 of its 49 were technology, so there
    #: was nowhere to diversify to and the cap could only ever be a
    #: name-count rule wearing a sector rule's clothes.
    #:
    #: Restored when the pool widened to a hundred across nine sectors,
    #: where it has somewhere to go. Measured over 3.65 years on hourly bars
    #: with per-bar spreads, eight names, monthly reselection:
    #:
    #:     cap    CAGR   max drawdown   return/drawdown   top sector
    #:     none   71.9%       -34.3%          2.10           88%
    #:     5      72.6%       -21.9%          3.32           62%
    #:     4      68.0%       -21.4%          3.18           50%
    #:     3      52.1%       -20.7%          2.51           38%
    #:     2      30.0%       -30.9%          0.97           25%
    #:
    #: Five keeps the return and takes a third off the drawdown. Tighter
    #: caps buy a little more drawdown for a lot of return, and three is
    #: already unbalanced across halves (67.7 then 38.1).
    #:
    #: The lesson is not that the earlier measurement was wrong but that a
    #: cap's value depends on what it caps. Uncapped, the screen picked
    #: eight technology names out of eight -- six of them semiconductors,
    #: three of those the same semicap equipment trade -- from a pool where
    #: only a third are technology.
    #:
    #: It remains a blunt instrument: "technology" lumps semiconductors,
    #: semicap and software together, so it cannot see that LRCX, AMAT and
    #: KLAC are one position. The per-instrument concentration cap cannot
    #: see it either. Correlation-aware sizing is the real answer and does
    #: not exist yet.
    max_per_sector: int | None = 5

    #: Weights. Volatility first since 2026-08-28, because the job is
    #: finding instruments about to move and volatility is the only one of
    #: the three that predicts it.
    #:
    #: Ranked against the size of the move over the following month, across
    #: 97 month-starts, the mean rank correlation is monotone in how much
    #: weight volatility carries: 0.084 at none, 0.235 at the old 0.35,
    #: 0.295 at 0.50, 0.327 at 0.70, 0.336 at all of it. Liquidity alone
    #: manages 0.084 and is right in 73% of months, which is barely a
    #: ranking at all. Paired month by month against the old weighting, this
    #: one is better in 83% of months at t=9.0.
    #:
    #: Backtested at eight names it is the drawdown that moves rather than
    #: the return: 7.2% CAGR against 7.4%, but -14.3% peak drawdown against
    #: -25.1%, and less spread paid. Monotone across all four weightings
    #: tried, which is why the direction is believed; the exact split is
    #: not, and 0.20/0.70/0.10 is chosen as the interior point rather than
    #: the measured optimum.
    #:
    #: Liquidity keeps a fifth rather than being dropped, though ranking on
    #: volatility alone measured marginally better. It is not there to
    #: forecast movement -- it is there because an instrument you cannot get
    #: out of at a sane price is not tradable whatever else is true of it,
    #: and the movement test is structurally blind to that: it measures how
    #: far a price went, not what it cost to follow. Removing the only term
    #: that represents cost, on the evidence of a test that cannot see cost,
    #: would be reading more from the number than it says.
    #:
    #: Continuity last and small -- it is a disqualifier more than a
    #: preference.
    weight_liquidity: float = 0.20
    weight_volatility: float = 0.70
    weight_continuity: float = 0.10

    #: Demote an instrument that is simultaneously volatile, dear and
    #: mean-reverting -- top quartile on volatility AND on spread, bottom
    #: quartile on variance ratio, all judged against the pool on the day.
    #:
    #: Each part is fine alone. Volatile is what the screen wants. Dear was
    #: measured as harmless on its own (see ``max_spread_multiple``, off for
    #: that reason). Choppy is weak. Together they describe an instrument
    #: that charges to enter and then does not go anywhere, which is the one
    #: combination a trend follower cannot survive.
    #:
    #: Measured point-in-time over 3.65 years, 100 candidates, eight slots:
    #: 72.6% -> 75.5% CAGR, -34.3% -> -33.1% drawdown, and both halves
    #: improve (79.6 -> 82.6, 66.1 -> 68.9). It changes ten selections in
    #: forty-three months and the whole effect is three names: COIN, INTC
    #: and PLTR leaving a month they would otherwise have been picked for.
    #:
    #: **That is a thin result and should be read as one.** Three names and
    #: ten events is a small sample, and the profile was designed after
    #: watching INTC misbehave, so it is not an out-of-sample test of the
    #: idea. Both halves improving is mild reassurance, not proof. What
    #: makes it worth keeping at that strength of evidence is the asymmetry:
    #: with a hundred candidates and eight slots, demoting a structurally
    #: poor instrument is close to free if the reasoning is wrong.
    #:
    #: An earlier version of this measured a clean zero and was written up
    #: as costing nothing. It was not running at all -- see
    #: ``_profile_flags``. The lesson kept here: a rule that reports its
    #: flags correctly can still be inert, and "no effect" is a claim that
    #: needs the same scepticism as a large one.
    #:
    #: Rolling, never a ban list. The profile is recomputed from trailing
    #: data at every screen, so a name un-flags itself when it changes.
    #: INTC is why that matters: 0.85x the median spread in 2023, 5.02x in
    #: 2025, 1.02x now. A static exclusion would have been wrong twice.
    hold_back_poor_profile: bool = True

    #: Refuse an instrument whose spread exceeds this multiple of the
    #: universe's own median on the day. ``None`` disables the rule.
    #:
    #: A gate rather than a weight, and that was measured rather than
    #: assumed. Tapering size by cost -- full weight up to the median, 0.7,
    #: then 0.4 -- did nothing beyond what the exclusion did, and tightening
    #: the threshold below 2.5x bought return with drawdown in a way that
    #: did not survive extension. What holds is that some exclusion beats
    #: none: past 2.5x, no position is small enough to make the instrument
    #: worth trading, so it is refused rather than shrunk. Excluding at 2.5x
    #: measured +11 points of return for 1.5 of drawdown and held under a
    #: within-sector control, which is what says it is a cost effect and not
    #: a bet against whichever sector happens to be dear.
    #:
    #: **Off since 2026-08-29.** The rule works and is tested; the default
    #: is ``None`` because it was measured to cost money.
    #:
    #: It shipped on 2026-08-28 justified by a backtest of the forty-nine
    #: name pool, where excluding above 2.5x was worth eleven points of
    #: return. Re-measured on a hundred-name pool over Capital's full 3.65
    #: years, trading every instrument and charging each its own per-bar
    #: spread, every threshold *loses* money:
    #:
    #:     gate     excluded   net kept   net given up
    #:     1.5x           10    101,265         17,181
    #:     2.0x            6    107,842         10,604
    #:     2.5x            1    116,598          1,848
    #:     none            0    118,446              0
    #:
    #: And it is not a matter of the level. Net per trade *rises* with
    #: spread -- 0.8 for names at or below the median, 0.9 from 1.0 to 1.5x,
    #: 1.5 above that -- while spread against net P&L correlates +0.069
    #: (p=0.50), which is nothing. The instruments that would have been
    #: refused made money in all four years, so no window exists in which
    #: this helped.
    #:
    #: What the original measurement actually found was that INTC and SMCI
    #: had a bad 2024-25, and they happened to be the dear names in a pool
    #: of forty-nine. Generalising "expensive" from two instruments was the
    #: error; a name is usually dear *because* it moves, and movement is
    #: what this strategy is paid for.
    #:
    #: The spread is still measured and still reported on every candidate --
    #: knowing what an instrument costs is worth having. It simply no longer
    #: refuses. Set a multiple to switch it back on.
    max_spread_multiple: float | None = None

    #: Calendar days of spread history the median is taken over. Trailing,
    #: never whole-history: INTC ran 0.85x the median in 2023, 3.63x in
    #: 2024, 5.02x in 2025 and 1.45x in 2026, so a static figure describes a
    #: period rather than an instrument and bans a name for a year it has
    #: already left behind.
    spread_lookback_days: int = 90

    #: Readings required before a spread median is acted on. Below it the
    #: figure is reported and the exclusion withheld -- the arithmetic is
    #: available, the claim is not.
    min_spread_readings: int = 25


@dataclass(frozen=True, slots=True)
class Candidate:
    """One instrument's measurements, and its score if it earned one."""

    ticker: str
    liquidity: float | None = None
    volatility: float | None = None
    continuity: float | None = None
    score: float | None = None
    sector: str = ""
    bars: int = 0
    #: Trailing median core-session spread, in basis points, and the same
    #: figure as a multiple of the universe's own median on the day.
    #: ``None`` means it could not be measured, which is reported as
    #: unverified rather than treated as cheap.
    spread_bp: float | None = None
    spread_multiple: float | None = None
    #: Why it is not ranked, empty when it is.
    unranked: str = ""

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """What the screen chose, what it rejected, and why."""

    as_of: str
    selected: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    sector_capped: tuple[str, ...] = ()
    #: Refused for cost, with the multiple that refused them.
    spread_excluded: tuple[str, ...] = ()
    #: Held back as volatile, dear and choppy at once.
    profile_held: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ranked(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.unranked == "")

    @property
    def unranked(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.unranked != "")


def _measure(closes, volumes, config: ScreenConfig) -> tuple[Any, Any, Any, int]:
    """Liquidity, volatility and continuity for one instrument. Pure."""
    import numpy as np

    bars = int(closes.notna().sum())
    if bars < config.min_history:
        return None, None, None, bars

    window_close = closes.dropna().tail(config.lookback)
    window_volume = (
        volumes.reindex(window_close.index) if volumes is not None else None
    )

    # Liquidity: turnover at the decision date, logged.
    #
    # Median *share* volume times the price on the last bar, rather than the
    # median of the daily turnover series. Those differ in a way that
    # matters: a stock that doubled through the window has doubling turnover
    # through it, so the path-median rewards having risen during the
    # measurement period. The first version of this did that, and the test
    # for "returns are not an input" caught it -- a stock that tripled
    # scored 0.92 against 0.07 for one that halved, with identical share
    # volume and volatility.
    #
    # Priced at as_of instead, the score depends on where the instrument is,
    # not on how it travelled there.
    #
    # Logged because the spread between the largest and smallest is orders
    # of magnitude, and an unlogged mean would make the ranking a referendum
    # on Apple.
    liquidity = None
    if window_volume is not None:
        shares = window_volume.dropna()
        last_price = float(window_close.iloc[-1])
        if len(shares) and last_price > 0:
            turnover = float(shares.median()) * last_price
            liquidity = float(np.log10(turnover)) if turnover > 0 else None

    # Volatility: annualised standard deviation of daily returns. A trend
    # system needs something that moves; this is not a risk penalty.
    returns = window_close.pct_change().dropna()
    volatility = (
        float(returns.std() * np.sqrt(252)) if len(returns) > 2 else None
    )

    # Continuity: how much of the window actually traded. A name with gaps
    # is one you cannot rely on being able to exit.
    continuity = float(len(window_close) / config.lookback) if config.lookback else None

    return liquidity, volatility, continuity, bars


def _z(values: Sequence[float]) -> list[float]:
    """Rank-based standard scores, so one outlier cannot own the ranking."""
    import numpy as np
    import pandas as pd

    if not len(values):
        return []
    ranks = pd.Series(values).rank(pct=True)
    # Centred on zero and roughly unit-scaled, which keeps the weights
    # meaning what they say.
    return list((ranks - 0.5) * 2.0)


def _apply_spread_gate(measured, spreads, cutoff, settings):
    """Refuse what costs too much, and name what could not be checked.

    Returns the candidates with ``spread_bp`` and ``spread_multiple``
    filled in, the tickers refused, and notes describing what happened --
    including how many could not be measured, because "no reading" and
    "read and fine" are different statements and only one is reassuring.

    The reference is the universe's own median on the day rather than a
    fixed number of basis points. Spreads move together: the universe
    median ran 13.94bp in 2023 and 9.63bp in 2026, so a fixed threshold
    would tighten and loosen with the market instead of with the
    instrument.
    """
    import numpy as np
    import pandas as pd

    if not spreads:
        return measured, (), ()

    readings: dict[str, tuple[float, int]] = {}
    horizon = (
        cutoff - pd.Timedelta(days=settings.spread_lookback_days)
        if cutoff is not None else None
    )
    for ticker, series in spreads.items():
        if series is None or getattr(series, "empty", True):
            continue
        index = pd.DatetimeIndex(series.index)
        if index.tz is None:
            index = index.tz_localize("UTC")
        window = pd.Series(np.asarray(series), index=index).dropna()
        if cutoff is not None:
            # Nothing after the decision date, and nothing older than the
            # lookback: a spread from two years ago is not evidence about
            # what this instrument costs now.
            window = window[(window.index <= cutoff) & (window.index > horizon)]
        if window.empty:
            continue
        readings[str(ticker)] = (float(window.median()), int(len(window)))

    if not readings:
        return measured, (), ("no spread readings available, so cost was not checked",)

    universe_median = float(np.median([value for value, _ in readings.values()]))
    if not np.isfinite(universe_median) or universe_median <= 0:
        return measured, (), ("universe spread median unusable, cost not checked",)

    ceiling = settings.max_spread_multiple
    refused: list[str] = []
    unverified: list[str] = []
    thin: list[str] = []
    updated: list[Candidate] = []

    for candidate in measured:
        reading = readings.get(candidate.ticker)
        if reading is None:
            if candidate.unranked == "":
                unverified.append(candidate.ticker)
            updated.append(candidate)
            continue

        value, count = reading
        multiple = value / universe_median
        enough = count >= settings.min_spread_readings
        if not enough and candidate.unranked == "":
            thin.append(candidate.ticker)

        refuse = (
            ceiling is not None
            and enough
            and multiple > ceiling
            and candidate.unranked == ""
        )
        updated.append(dataclasses.replace(
            candidate,
            spread_bp=round(value, 2),
            spread_multiple=round(multiple, 2),
            unranked=(
                f"spread {multiple:.2f}x the universe median"
                if refuse else candidate.unranked
            ),
        ))
        if refuse:
            refused.append(candidate.ticker)

    notes: list[str] = [
        f"universe median spread {universe_median:.2f}bp over {len(readings)} instruments"
    ]
    if refused:
        notes.append(
            f"refused for cost above {ceiling}x: {', '.join(sorted(refused))}"
        )
    if unverified:
        notes.append(
            f"spread unverified for {len(unverified)}: {', '.join(sorted(unverified))}"
        )
    if thin:
        notes.append(
            f"spread read but below {settings.min_spread_readings} readings, "
            f"exclusion withheld: {', '.join(sorted(thin))}"
        )
    return updated, tuple(refused), tuple(notes)


def _profile_flags(measured, prices, spreads, cutoff, settings):
    """Name what is volatile, dear and mean-reverting at once.

    Identifies only. The demotion is applied after scoring, in
    ``rank_candidates``, and that separation is not cosmetic: this ran
    before the scores existed and multiplied ``None``, which the scoring
    pass then overwrote outright. The rule reported flags and changed
    nothing for as long as it existed, and a backtest of it measured a
    clean zero -- which read as "costs nothing" rather than "never ran".

    A demotion rather than a refusal, which is the right strength for the
    evidence: the condition is too rare to test properly, so a name far
    ahead of the alternatives should still be able to win despite it.

    Judged against the pool on the day rather than against fixed numbers.
    The whole market widens and calms together, so a fixed threshold would
    drift with the market instead of with the instrument.

    Choppiness is a **variance ratio** -- the variance of five-bar returns
    against five times the variance of one-bar returns -- and not the more
    obvious net-move-over-path-travelled. Path efficiency needs a cumulative
    return, which encodes which way the instrument went, and directional
    performance must not reach this screen at any strength: ranking on it
    would quietly turn the universe into "hold whatever went up". A variance
    ratio is built from dispersions and is blind to sign. Below one is
    mean-reverting, above one trending.

    Everything is measured at or before ``cutoff``, so a name un-flags
    itself as its behaviour changes. Deliberately not a ban list.
    """
    import numpy as np
    import pandas as pd

    if not getattr(settings, "hold_back_poor_profile", False):
        return (), ()
    if not prices:
        return (), ()

    rows = {}
    for candidate in measured:
        if candidate.unranked != "" or candidate.spread_bp is None:
            continue
        frame = (prices or {}).get(candidate.ticker)
        if frame is None or getattr(frame, "empty", True) or "close" not in frame:
            continue
        index = pd.DatetimeIndex(frame.index)
        if index.tz is None:
            index = index.tz_localize("UTC")
        series = pd.Series(np.asarray(frame["close"]), index=index).dropna()
        if cutoff is not None:
            series = series[series.index <= cutoff]
        series = series.tail(settings.lookback)
        if len(series) < max(40, settings.lookback // 4):
            continue
        steps = series.pct_change().dropna()
        single = float(steps.var())
        if single <= 0:
            continue
        multi = float(steps.rolling(5).sum().dropna().var())
        rows[candidate.ticker] = {
            "vol": float(steps.std()),
            "variance_ratio": multi / (5.0 * single),
            "spread": float(candidate.spread_bp),
        }

    if len(rows) < 20:          # too few to form quantiles honestly
        return (), ()
    frame = pd.DataFrame(rows).T
    flagged = set(frame.index[
        (frame.vol >= frame.vol.quantile(0.75))
        & (frame.spread >= frame.spread.quantile(0.75))
        & (frame.variance_ratio <= frame.variance_ratio.quantile(0.25))
    ])
    if not flagged:
        return (), ()
    return tuple(sorted(flagged)), (
        f"demoted as volatile, dear and mean-reverting: {', '.join(sorted(flagged))}",
    )


def rank_candidates(
    prices: Mapping[str, Any],
    as_of=None,
    config: ScreenConfig | None = None,
    sectors: Mapping[str, str] | None = None,
    spreads: Mapping[str, Any] | None = None,
) -> ScreenResult:
    """Score every candidate. Pure, and never looks past ``as_of``.

    ``prices`` maps a ticker to a frame carrying ``close`` and ``volume``,
    indexed by timestamp.
    """
    import pandas as pd

    settings = config or ScreenConfig()
    cutoff = pd.Timestamp(as_of) if as_of is not None else None
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")

    measured: list[Candidate] = []
    for ticker, frame in sorted((prices or {}).items()):
        sector = str((sectors or {}).get(ticker, "") or "")
        if frame is None or getattr(frame, "empty", True):
            measured.append(Candidate(ticker, sector=sector, unranked="no price data"))
            continue

        working = frame
        if cutoff is not None:
            index = pd.DatetimeIndex(working.index)
            if index.tz is None:
                index = index.tz_localize("UTC")
            # The whole point: nothing after the decision date.
            working = working[index <= cutoff]

        closes = working["close"] if "close" in working else None
        volumes = working["volume"] if "volume" in working else None
        if closes is None or closes.dropna().empty:
            measured.append(Candidate(ticker, sector=sector, unranked="no closes"))
            continue

        liquidity, volatility, continuity, bars = _measure(closes, volumes, settings)
        if liquidity is None or volatility is None:
            measured.append(Candidate(
                ticker, sector=sector, bars=bars,
                unranked=(
                    f"only {bars} bars, needs {settings.min_history}"
                    if bars < settings.min_history else "no usable volume"
                ),
            ))
            continue

        measured.append(Candidate(
            ticker, liquidity=liquidity, volatility=volatility,
            continuity=continuity, sector=sector, bars=bars,
        ))

    # ---- cost, before the ranking rather than after
    #
    # An instrument refused for cost must not influence the standard scores
    # of the ones that survive: the z-scores are rank-based, so leaving a
    # name in the pool that cannot be traded would shift everything else's
    # position for no reason. Refusing first is also the honest order --
    # affordability is a precondition, not a tie-breaker.
    measured, spread_excluded, spread_notes = _apply_spread_gate(
        measured, spreads, cutoff, settings
    )
    profile_held, profile_notes = _profile_flags(
        measured, prices, spreads, cutoff, settings
    )

    rankable = [c for c in measured if c.unranked == ""]
    if rankable:
        zl = _z([c.liquidity for c in rankable])
        zv = _z([c.volatility for c in rankable])
        zc = _z([c.continuity or 0.0 for c in rankable])
        scored = []
        for candidate, a, b, c_ in zip(rankable, zl, zv, zc):
            score = (
                settings.weight_liquidity * a
                + settings.weight_volatility * b
                + settings.weight_continuity * c_
            )
            # `replace` rather than a positional rebuild. The positional
            # form silently mis-assigned the moment a field was added
            # before `unranked`: the trailing "" landed in `spread_bp` and
            # the measured spread was dropped, with no error until
            # something tried to format the result as a number.
            scored.append(dataclasses.replace(candidate, score=float(score)))
        by_ticker = {c.ticker: c for c in scored}
        measured = [by_ticker.get(c.ticker, c) for c in measured]

    # The demotion, applied *after* scoring because there is nothing to
    # halve before it. Not a refusal: a name far enough ahead of the
    # alternatives still wins despite the profile.
    if profile_held:
        held = set(profile_held)
        measured = [
            dataclasses.replace(c, score=float(c.score) * PROFILE_DEMOTION)
            if c.ticker in held and c.score is not None else c
            for c in measured
        ]

    ordered = sorted(
        (c for c in measured if c.unranked == ""),
        key=lambda c: (-(c.score or 0.0), c.ticker),
    )

    notes: list[str] = []
    selected: list[str] = []
    capped: list[str] = []
    per_sector: dict[str, int] = {}

    cap = settings.max_per_sector
    if cap is not None and not (sectors or {}):
        cap = None
        notes.append(
            "no sector map supplied, so the sector cap was not applied"
        )

    for candidate in ordered:
        if len(selected) >= settings.target:
            break
        if cap is not None and candidate.sector:
            if per_sector.get(candidate.sector, 0) >= cap:
                capped.append(candidate.ticker)
                continue
            per_sector[candidate.sector] = per_sector.get(candidate.sector, 0) + 1
        selected.append(candidate.ticker)

    if len(selected) < settings.target:
        notes.append(
            f"only {len(selected)} of {settings.target} could be filled from "
            f"{len(ordered)} ranked candidates"
        )

    return ScreenResult(
        as_of=str(cutoff or ""), selected=tuple(selected),
        candidates=tuple(measured), sector_capped=tuple(capped),
        spread_excluded=spread_excluded,
        profile_held=profile_held,
        notes=tuple([*spread_notes, *profile_notes, *notes]),
    )


def select_universe(
    repository,
    candidates: Sequence[str],
    as_of=None,
    config: ScreenConfig | None = None,
    sectors: Mapping[str, str] | None = None,
    interval: str = "1d",
    app_config=None,
    account: str | None = None,
) -> ScreenResult:
    """Read the lake and rank. Daily bars regardless of what the engine trades.

    Liquidity and volatility are properties of an instrument, not of the bar
    size somebody happens to be trading it on, and daily bars have years of
    history where hourly has two. Measuring the screen on the trading
    interval would tie the universe to a decision it has nothing to do with.
    """
    frames: dict[str, Any] = {}
    try:
        market = repository.load_market(tickers=list(candidates), interval=interval)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read the lake for screening: %s", error)
        return ScreenResult(as_of=str(as_of or ""), notes=(f"lake unreadable: {error}",))

    if market is None or getattr(market, "empty", True):
        return ScreenResult(as_of=str(as_of or ""), notes=("the lake returned nothing",))

    working = market.reset_index()
    ticker_column = "ticker" if "ticker" in working.columns else working.columns[1]
    stamp_column = "timestamp" if "timestamp" in working.columns else working.columns[0]
    for ticker, group in working.groupby(ticker_column):
        frames[str(ticker)] = group.set_index(stamp_column).sort_index()

    for ticker in candidates:
        frames.setdefault(str(ticker), None)

    return rank_candidates(
        frames, as_of=as_of, config=config, sectors=sectors,
        spreads=_spread_series(repository, candidates, app_config, account),
    )


#: Bars carrying a measured spread. Capital's, because they are the only
#: ones that record bid and offer -- the vendor's daily bars carry a close
#: and nothing about what it would have cost to transact at it.
SPREAD_INTERVAL = "capital_1h"

#: Hours, in New York time, whose spreads describe the session the strategy
#: deals in. The opening hour is excluded deliberately: its median runs
#: 1.22x the core session's, so including it would rank instruments on an
#: auction most of their trading does not happen in.
SPREAD_HOURS = (10, 15)


def _spread_series(repository, candidates, config=None, account=None) -> dict[str, Any]:
    """Core-session spread per instrument per day, or ``{}``.

    Two stores hold this fact and neither is wrong. ``capital_1h`` bars
    carry a measured spread on every bar and are the richer source, but they
    are a research archive: nothing on a deployment writes them. The live
    reading is ``persist.spreads``, one observation per instrument per day,
    appended by ``record-candidate-spreads``.

    The lake is preferred where it exists and the observations are read
    otherwise, and the source is logged either way. A deployment ran for a
    month with a screen reading the archive it did not have while the store
    it did have filled up beside it -- the cost gate and the profile
    demotion both dormant, and reporting "cost unverified" perfectly
    correctly the whole time.

    Never raises and never partially fails the screen: an unreadable spread
    store means cost is reported unverified, which the ranking handles, and
    is a great deal better than a screen that stops running.
    """
    import pandas as pd

    from_lake = _spread_series_from_lake(repository, candidates)
    if from_lake:
        logger.info(
            "Screening spreads: %d instruments from the %s archive",
            len(from_lake), SPREAD_INTERVAL,
        )
        return from_lake
    live = _spread_series_from_observations(candidates, config, account)
    if live:
        logger.info(
            "Screening spreads: %d instruments from recorded observations",
            len(live),
        )
    return live


def _spread_series_from_observations(candidates, config=None, account=None):
    """The live store: one reading per instrument per day.

    Readings taken with the exchange shut are dropped rather than averaged
    in. An out-of-hours quote is several times the session spread on a name
    nobody is making a market in, so including them would rank instruments
    on a market the strategy never deals in -- the same reason
    ``SPREAD_HOURS`` excludes the opening auction.
    """
    import pandas as pd

    try:
        from cfdinvest.persist import spreads as spread_store

        rows = spread_store.load_spreads(config, account)
    except Exception as error:  # noqa: BLE001
        logger.warning("Recorded spreads unreadable: %s", error)
        return {}
    if not rows:
        return {}

    wanted = {str(t) for t in (candidates or ())}
    kept: dict[str, list[tuple[Any, float]]] = {}
    shut = 0
    for row in rows:
        ticker = str((row or {}).get("ticker") or "")
        if ticker not in wanted:
            continue
        value = (row or {}).get("spread_bp")
        if value is None:
            continue
        if not (row.get("session_open") and row.get("market_status")):
            shut += 1
            continue
        try:
            stamp = pd.Timestamp(row.get("at"))
            reading = float(value)
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        local = stamp.tz_convert("America/New_York")
        low, high = SPREAD_HOURS
        if not (low <= local.hour <= high):
            continue
        kept.setdefault(ticker, []).append((local.normalize().tz_localize(None), reading))

    if shut:
        logger.info(
            "Ignored %d spread reading(s) taken with the exchange shut", shut
        )
    out: dict[str, Any] = {}
    for ticker, pairs in kept.items():
        series = pd.Series(
            [v for _, v in pairs], index=pd.DatetimeIndex([d for d, _ in pairs])
        )
        daily = series.groupby(series.index).median().dropna()
        if not daily.empty:
            out[ticker] = daily
    return out


def _spread_series_from_lake(repository, candidates) -> dict[str, Any]:
    """The research archive: a measured spread on every bar."""
    import pandas as pd

    try:
        bars = repository.load_market(
            tickers=list(candidates), interval=SPREAD_INTERVAL
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("No spread bars for screening: %s", error)
        return {}
    if bars is None or getattr(bars, "empty", True):
        logger.debug("No %s bars; falling back to recorded observations",
                     SPREAD_INTERVAL)
        return {}

    working = bars.reset_index()
    if "spread_bp" not in working.columns:
        logger.warning("Spread bars carry no spread_bp column")
        return {}
    ticker_column = "ticker" if "ticker" in working.columns else working.columns[1]
    stamp_column = "timestamp" if "timestamp" in working.columns else working.columns[0]

    out: dict[str, Any] = {}
    for ticker, group in working.groupby(ticker_column):
        frame = group.set_index(stamp_column).sort_index()
        index = pd.DatetimeIndex(frame.index)
        if index.tz is None:
            index = index.tz_localize("UTC")
        local = index.tz_convert("America/New_York")
        low, high = SPREAD_HOURS
        core = frame[(local.hour >= low) & (local.hour <= high)]
        if core.empty:
            continue
        # One reading a day, so a name quoted more often does not simply
        # outvote a name quoted less.
        daily = core["spread_bp"].groupby(
            pd.DatetimeIndex(core.index).tz_localize(None).normalize()
        ).median().dropna()
        if not daily.empty:
            out[str(ticker)] = daily
    return out


#: What a flagged instrument's score is multiplied by.
#:
#: **This number is not tuned, and the measurement says it cannot be.**
#: Swept over 0.75, 0.60, 0.50, 0.40, 0.25 and 0.0 on the production pool,
#: every value returned an identical 75.5% CAGR and the same ten changed
#: selections. With a hundred candidates the scores are packed tightly
#: enough that even a quarter off displaces a flagged name from the top
#: eight, and nothing short of that keeps it in. At this pool size a
#: demotion and a refusal are the same policy.
#:
#: It stays a multiplier rather than becoming an exclusion because the two
#: only coincide while the pool is large and densely scored. On a smaller
#: or more spread-out pool a name far enough ahead of the alternatives
#: should still be able to win despite the profile, and a refusal could
#: never allow that. The gentler form is kept for the case where the
#: difference starts to exist -- not because it has been shown to matter.
PROFILE_DEMOTION: float = 0.5

#: Where selection snapshots live, under the account root.
SCREEN_DIR = "universe"


def record_screen(result: ScreenResult, config=None, account: str | None = None) -> int:
    """Append this selection and everything it rejected. Never raises.

    Append-only, like every other observation here. A selection is a
    decision taken at a moment on evidence that will have moved by the time
    anybody asks about it, and one that can be edited afterwards is not
    evidence of anything.

    The rejected candidates travel with the chosen ones deliberately. Being
    able to re-run the rule is not the same as being able to see what it saw
    -- recomputing the ranking later reads a lake that has changed, and
    would quietly justify whatever was chosen.
    """
    import json
    from pathlib import Path

    if result is None:
        return 0
    try:
        from cfdinvest.persist.storage import account_root

        path = Path(account_root(config, account)) / SCREEN_DIR / "selections.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "as_of": result.as_of,
            "selected": list(result.selected),
            "sector_capped": list(result.sector_capped),
            "notes": list(result.notes),
            "candidates": [c.as_row() for c in result.candidates],
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return 1
    except Exception as error:  # noqa: BLE001 - recording must not fail a screen
        logger.warning("Could not record the selection: %s", error)
        return 0


def load_screens(config=None, account: str | None = None) -> list[dict]:
    """Every selection ever made, oldest first. Never raises."""
    import json
    from pathlib import Path

    try:
        from cfdinvest.persist.storage import account_root

        path = Path(account_root(config, account)) / SCREEN_DIR / "selections.jsonl"
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping a malformed selection in %s", path)
        return rows
    except Exception as error:  # noqa: BLE001
        logger.warning("Selections unreadable: %s", error)
        return []
