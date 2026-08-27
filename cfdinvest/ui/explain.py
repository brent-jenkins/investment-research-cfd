"""Turning the pipeline's internal vocabulary into English.

The signal layer records why it decided what it decided, in a compact machine
format: ``rule[|flag...][ @regime]``, so ``trend_short|high_volatility
@crisis``. That is exactly right for a log and exactly wrong for a person
deciding whether to place a trade at nine in the evening.

This module is the dictionary between the two. It is deliberately a lookup
rather than a clever parser: every token the pipeline can emit is enumerable,
so an unknown token means either a new rule upstream or a typo here — and in
both cases the honest response is to show the raw token rather than guess.
Unknown tokens are therefore tidied (underscores to spaces) and passed
through, never dropped.

Pure, and free of Streamlit: every function takes a string and returns a
string.
"""

from __future__ import annotations

__all__ = [
    "RULES",
    "FLAGS",
    "REGIMES",
    "ACTIONS",
    "BREACHES",
    "reason",
    "action",
    "breach",
    "outcome",
    "phrase",
]

#: The direction rules, from ``signals.combined.DIRECTION_PRECEDENCE``.
#: Each reads as a complete sentence about what the model decided.
RULES: dict[str, str] = {
    "trend_long": "Uptrend, and the filters allow a long position",
    "trend_short": "Downtrend, and the filters allow a short position",
    "blocked_long": "An uptrend, but the regime filters block going long",
    "blocked_short": "A downtrend, but the regime filters block going short",
    "below_entry_threshold": "The trend is too weak to act on",
    "below_min_size": (
        "Allowed, but the position would be too small to be worth the spread"
    ),
    "no_data": "No trend reading yet — warming up, or a gap in the price history",
    "default_flat": "No rule matched, so nothing was taken",
    "unknown": "The reason was not recorded",
}

#: The filter flags, from ``signals.filters.REASON_FLAGS``. Written as
#: clauses, because they always follow a rule.
FLAGS: dict[str, str] = {
    "high_volatility": "volatility is high, so the size was cut",
    "correlation_breakdown": "correlations have broken down",
    "inflation_tilt": "inflation is rising",
    "labor_risk_off": "the labour market is weakening",
    "contraction_blocks_equity_long": (
        "an economic contraction blocks long equity positions"
    ),
}

#: The combined market regimes, from ``regimes.combined.COMBINED_REGIMES``.
REGIMES: dict[str, str] = {
    "crisis": "Crisis",
    "stagflation": "Stagflation",
    "risk_off": "Risk-off",
    "neutral": "Neutral",
    "risk_on": "Risk-on",
    "expansion": "Expansion",
    "unknown": "Unclassified",
}

#: Trade-log actions, from ``execution.state.ACTIONS``, plus the reasons the
#: engine records alongside them.
ACTIONS: dict[str, str] = {
    "hold": "Hold",
    "open_long": "Open a long position",
    "open_short": "Open a short position",
    "close": "Close the position",
    "flip_long_to_short": "Flip from long to short",
    "flip_short_to_long": "Flip from short to long",
    "resize": "Adjust the position size",
    "stop_loss": "Stopped out at the stop-loss level",
    "take_profit": "Closed at the take-profit level",
    "signal": "The model changed its mind",
    "time_stop": "Held long enough without working, and closed",
    "stop_out": "Account-level stop-out — everything was flattened",
}

#: How a stop-loss or take-profit level was reached.
BREACHES: dict[str, str] = {
    "intrabar": "traded through the level during the bar",
    "gap": "gapped past the level — filled at the open, not at the level",
    "": "",
}

#: Run outcomes recorded in a run summary.
OUTCOMES: dict[str, str] = {
    "ok": "Succeeded",
    "failed": "Failed",
    "empty": "No data to trade",
    "unreadable": "Summary could not be read",
}


def phrase(token: str, vocabulary: dict[str, str]) -> str:
    """Look a token up, falling back to a tidied version of itself.

    An unrecognised token is shown, not hidden: it means the pipeline grew a
    new one, and silently dropping it would make the reason wrong rather than
    merely unfamiliar.
    """
    key = str(token or "").strip()
    if key in vocabulary:
        return vocabulary[key]
    return key.replace("_", " ")



#: How the macro series were fetched. The raw tokens are the ingestion
#: layer's own and read as identifiers on a card -- and "publication_lag" has
#: no space to break at, so it wrapped mid-word.
VINTAGE_MODES: dict[str, str] = {
    "publication_lag": "Publication lag",
    "initial_release": "Initial release",
    "latest": "Latest revision",
}


def vintage(name: str | None) -> str:
    """A vintage mode in words: ``publication_lag`` -> ``Publication lag``."""
    text = phrase(name, VINTAGE_MODES)
    return text[:1].upper() + text[1:] if text else text

def reason(text: str | None) -> str:
    """Translate a signal reason into a sentence.

    ``trend_short|high_volatility @crisis`` becomes
    "Downtrend, and the filters allow a short position — volatility is high,
    so the size was cut. Market regime: Crisis."
    """
    raw = str(text or "").strip()
    if not raw:
        return "No reason recorded."

    body, _, regime = raw.partition("@")
    rule, *flags = [part.strip() for part in body.strip().split("|") if part.strip()]

    sentence = phrase(rule, RULES)
    if flags:
        clauses = [phrase(flag, FLAGS) for flag in flags]
        if len(clauses) == 1:
            sentence += f" — {clauses[0]}"
        else:
            sentence += f" — {', '.join(clauses[:-1])}, and {clauses[-1]}"
    sentence = sentence.rstrip(".") + "."

    regime = regime.strip()
    if regime:
        sentence += f" Market regime: {phrase(regime, REGIMES)}."
    return sentence


def action(name: str | None) -> str:
    """Translate a trade-log action or reason."""
    return phrase(name, ACTIONS).capitalize() if name else ""


def breach(name: str | None) -> str:
    """Translate how a level was reached."""
    return phrase(name, BREACHES)


def outcome(name: str | None) -> str:
    """Translate a run's recorded status."""
    return phrase(name, OUTCOMES).capitalize() if name else ""
