"""Research harnesses, kept in the repository rather than in a console.

This package exists because of what was missing rather than what was wrong.
The 2026-09-08 battery chose the universe the account trades -- cap 8,
target 50, adopted on an arm returning 191.0% at 6.6% -- and it was run from
a scratch script that no longer exists. On 2026-09-12 nothing here could
regenerate that figure, so the shipped configuration rested on a number that
could not be reproduced, and every parameter question was blocked behind it.

"One engine, and it is the production one" covers the ENGINE. The engine is
shared and corrected. It said nothing about the HARNESS, and the harness is
what produced every headline figure in CLAUDE.md.

So a sweep lives here, is committed, and refuses to report an arm until its
baseline reproduces a recorded result.
"""

from cfdinvest.research.harness import (
    RECORDED,
    ReproductionError,
    arm_metrics,
    assert_reproduces,
    membership_mask,
    research_backtest,
    run_arm,
    screen_selections,
)

__all__ = [
    "RECORDED",
    "ReproductionError",
    "arm_metrics",
    "assert_reproduces",
    "membership_mask",
    "research_backtest",
    "run_arm",
    "screen_selections",
]
