"""The thing you actually read before typing into Trading 212.

Plain text, fixed layout, no colour and no markup — it has to be legible in a
terminal, in an email, and on a phone at the moment of trading. The order is
deliberate: **the verdict first, the instruction second, the numbers third.**
Anyone reading this is about to act, and the arithmetic is what they came to
avoid doing.

Nothing here decides anything. It renders what
:mod:`cfdinvest.livehelper.safety` and
:mod:`cfdinvest.livehelper.sizing_helper` already worked out, and it never
softens a verdict: an ``UNSAFE`` trade says so at the top, in those letters.
"""

from __future__ import annotations

import textwrap

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.intent import TradeIntent
from cfdinvest.livehelper.safety import TradeSafetyAssessment, _finite
from cfdinvest.livehelper.sizing_helper import SizingRecommendation

__all__ = ["DISCLAIMER", "build_execution_helper_summary"]

WIDTH = 68

#: Printed on every summary. This tool models a trade; it does not place one,
#: and its numbers are only as good as the inputs typed into it.
DISCLAIMER = (
    "This is a decision aid, not an instruction. Nothing here has been sent "
    "anywhere: no order is placed, no Trading 212 interface is touched, and "
    "every figure rests on the prices and rates supplied above. Check them "
    "against the platform before you act."
)

#: What each verdict means, so the word is never read as a mood.
_VERDICT_TEXT: dict[str, str] = {
    "SAFE": "Inside every limit you set.",
    "BORDERLINE": (
        "Inside your hard limits, but something is close to one or could not "
        "be checked. Read the reasons before acting."
    ),
    "UNSAFE": "Outside at least one limit you set. Do not place as written.",
}


def _rule(character: str = "-") -> str:
    return character * WIDTH


def _bullet(text: str, indent: str = "    ") -> list[str]:
    """A wrapped bullet, hanging under its dash.

    Bullets used to be emitted unwrapped, which worked only for as long as
    every message happened to be short. One long recommendation produced a
    185-character line in a block that is otherwise 68 wide -- and a mail
    client that does not soft-wrap turns that into a single unreadable row.
    """
    return textwrap.wrap(
        text,
        width=WIDTH,
        initial_indent=f"{indent}- ",
        subsequent_indent=f"{indent}  ",
    ) or [f"{indent}- {text}"]


def _wrap(text: str, indent: str = "  ") -> list[str]:
    """Fold a paragraph to the summary width.

    Done here rather than left to the terminal: this text is also emailed,
    and a mail client that does not soft-wrap turns one long line into a
    horizontal scroll nobody reads.
    """
    return textwrap.wrap(
        text, width=WIDTH, initial_indent=indent, subsequent_indent=indent
    )


def _money(value, currency: str = "£") -> str:
    if not _finite(value):
        return "not computable"
    sign = "-" if value < 0 else ""
    return f"{sign}{currency}{abs(value):,.2f}"


def _percent(value) -> str:
    return f"{value:.1%}" if _finite(value) else "not computable"


def _number(value, places: int = 4) -> str:
    return f"{value:,.{places}f}" if _finite(value) else "not computable"


def _row(label: str, value: str, limit: str = "") -> str:
    """One aligned label/value line, with an optional limit column."""
    body = f"  {label:<26} {value:>18}"
    return f"{body}   {limit}" if limit else body


def build_execution_helper_summary(
    trade_intent: TradeIntent,
    safety_assessment: TradeSafetyAssessment,
    sizing_recommendation: SizingRecommendation | None = None,
    *,
    config: LiveHelperConfig | None = None,
) -> str:
    """Render everything into one readable block.

    Pure — it returns a string and prints nothing, so the caller decides
    whether it goes to a terminal, an email or a web page.
    """
    limits = config or LiveHelperConfig()
    lines: list[str] = []

    # -- verdict, first, in its own words ---------------------------------- #
    lines.append(_rule("="))
    lines.append(f"  {safety_assessment.status}   {trade_intent.instrument} "
                 f"{trade_intent.direction.upper()}")
    # Wrapped like everything else. The BORDERLINE line is the long one, and
    # it ran to 113 characters in a block that is otherwise 68 wide.
    lines.extend(_wrap(_VERDICT_TEXT.get(safety_assessment.status, "")))
    lines.append(_rule("="))

    # -- what to do -------------------------------------------------------- #
    actions = list(safety_assessment.recommendations)
    if sizing_recommendation is not None and sizing_recommendation.reduces:
        # A specific number supersedes every generic "reduce quantity": three
        # lines all saying trim it, one of which says by how much, reads as
        # three problems rather than as one instruction.
        actions = [a for a in actions if not a.startswith("Reduce quantity")]
        actions.insert(
            0,
            f"Reduce quantity to {sizing_recommendation.recommended_quantity:,.4f}"
            f" (from {trade_intent.quantity:,.4f}).",
        )
    if actions:
        lines.append("")
        lines.append("  WHAT TO DO")
        for action in actions:
            lines.extend(_bullet(action))

    # -- the trade as written ---------------------------------------------- #
    lines.append("")
    lines.append("  THE TRADE AS WRITTEN")
    lines.append(_row("Instrument", trade_intent.instrument))
    lines.append(_row("Direction", trade_intent.direction))
    lines.append(_row("Quantity", _number(trade_intent.quantity)))
    lines.append(
        _row(
            "Entry",
            _number(trade_intent.entry_price) if trade_intent.entry_price
            else "at market",
        )
    )
    lines.append(
        _row("Stop-loss", _number(trade_intent.stop_loss)
             if trade_intent.has_stop else "NONE SET")
    )
    lines.append(
        _row("Take-profit", _number(trade_intent.take_profit)
             if trade_intent.take_profit else "none")
    )
    if trade_intent.is_foreign:
        lines.append(
            _row("Quoted in", f"{trade_intent.instrument_currency}"
                 f" (account {trade_intent.account_currency})")
        )

    # -- what it costs and risks ------------------------------------------- #
    lines.append("")
    lines.append("  WHAT IT RISKS")
    lines.append(
        _row("Risk to stop", _money(safety_assessment.risk_gbp),
             f"limit {_money(limits.max_risk_per_trade_gbp)}")
    )
    lines.append(
        _row("Exposure", _money(safety_assessment.exposure_gbp),
             f"limit {_money(limits.max_exposure_per_instrument_gbp)}")
    )
    lines.append(
        _row("Effective leverage",
             f"{safety_assessment.leverage:,.2f}x"
             if _finite(safety_assessment.leverage) else "not computable",
             f"limit {limits.max_leverage_per_trade:,.0f}x")
    )
    lines.append(
        _row("Margin required", _money(safety_assessment.margin_required_gbp),
             f"instrument leverage "
             f"{_number(safety_assessment.instrument_leverage, 0)}x")
    )
    lines.append(
        _row("Margin used after",
             _percent(safety_assessment.margin_usage_pct_after_trade),
             f"limit {limits.max_margin_usage_pct_after_trade:.0%}")
    )
    if trade_intent.is_foreign:
        lines.append(
            _row("Currency exposure", _percent(safety_assessment.fx_exposure_pct),
                 f"limit {limits.max_fx_exposure_pct:.0%}")
        )
    if _finite(safety_assessment.reward_risk_ratio):
        lines.append(
            _row("Reward to risk", f"{safety_assessment.reward_risk_ratio:,.2f}")
        )

    # -- conditions --------------------------------------------------------- #
    conditions = []
    if safety_assessment.volatility_flag:
        conditions.append("volatility above your threshold")
    if safety_assessment.spread_flag:
        conditions.append("spread wide against the stop distance")
    lines.append("")
    lines.append("  CONDITIONS")
    lines.append(
        f"    {'; '.join(conditions) if conditions else 'nothing flagged'}"
    )

    # -- safe size ---------------------------------------------------------- #
    if sizing_recommendation is not None:
        lines.append("")
        lines.append("  SAFE SIZE")
        if not sizing_recommendation.tradeable:
            lines.append("    No size clears every limit.")
        else:
            lines.append(
                _row("Recommended quantity",
                     _number(sizing_recommendation.recommended_quantity))
            )
            lines.append(
                _row("Its exposure",
                     _money(sizing_recommendation.recommended_exposure_gbp))
            )
            lines.append(
                _row("Its risk",
                     _money(sizing_recommendation.recommended_risk_gbp))
            )
            lines.append(
                _row("Its margin",
                     _money(sizing_recommendation.margin_required_gbp))
            )
            if sizing_recommendation.binding_constraint:
                lines.append(
                    f"    Bound by: {sizing_recommendation.binding_constraint}"
                )
        for note in sizing_recommendation.notes:
            lines.extend(_wrap(note, indent="    "))

    # -- why ----------------------------------------------------------------- #
    if safety_assessment.reasons:
        lines.append("")
        lines.append("  WHY")
        for reason in safety_assessment.reasons:
            lines.extend(_bullet(reason))

    if safety_assessment.unknowns:
        lines.append("")
        lines.append("  NOT CHECKED")
        lines.append(
            f"    {', '.join(safety_assessment.unknowns)} — supply these for a "
            "complete assessment. Until then this cannot be graded SAFE."
        )

    if trade_intent.notes:
        lines.append("")
        lines.append(f"  NOTE: {trade_intent.notes}")

    lines.append("")
    lines.append(_rule())
    lines.extend(_wrap(DISCLAIMER))
    lines.append(_rule())

    return "\n".join(lines)
