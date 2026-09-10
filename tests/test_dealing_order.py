"""Closes go to the broker before opens.

A close frees margin; an open commits it. Placed in the order they were
built -- roughly ticker order -- an open can be sent before a close that
would have funded it, and Capital rejects it. The rejection reads as a
broker error rather than as an ordering mistake, which is the kind of fault
that survives a long time.

It has never bitten, and the reason is worth stating because it is not
reassuring: `max_margin_usage` is unset and the gross ceiling has never
fired on the account. The ordering was correct by coincidence, and the
coincidence ends the day either limit is switched on.

Nothing about the DECISION changes. `plan_orders` already sees every intent
at once and applies its caps across all of them; this is only the sequence
in which the account is actually moved. That is why it needs no measurement:
it is a safety property, not a strategy change.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M480 build order preserved, so an open can precede a close
  M481 opens ordered first, which is the failure inverted
  M482 the sort dropping non-acting intents' exclusion, so refusals are placed
  M483 an unstable sort, so a pass is not reproducible from its own record
  M484 a halted report still returning work
"""

from __future__ import annotations

from cfdinvest.broker.capital.bridge import BridgeReport, Intent


def intent(ticker, action):
    return Intent(
        ticker=ticker, epic=ticker, direction="SELL",
        held_units=1.0 if action in ("close", "resize") else 0.0,
        target_units=0.0 if action == "close" else 1.0,
        action=action, price=100.0,
    )


def actions(report):
    return [i.action for i in report.actionable]


def test_every_close_precedes_every_open():
    """M480/M481: the property, stated as plainly as it can be.

    Built in an order that interleaves them, so a report that merely
    preserves build order fails rather than passing by luck.
    """
    report = BridgeReport(intents=(
        intent("AAA", "open"), intent("BBB", "close"), intent("CCC", "open"),
        intent("DDD", "resize"), intent("EEE", "close"),
    ))

    order = actions(report)
    assert order == ["close", "close", "resize", "open", "open"], order

    last_close = max(i for i, a in enumerate(order) if a == "close")
    first_open = min(i for i, a in enumerate(order) if a == "open")
    assert last_close < first_open, (
        "an open is placed before a close that could have funded it"
    )


def test_a_resize_sits_between_them():
    """A resize may release margin or commit it, and its delta is usually
    small. Between the two is the honest place for something that could be
    either, and it must not be lumped in with opens."""
    report = BridgeReport(intents=(
        intent("AAA", "open"), intent("BBB", "resize"), intent("CCC", "close"),
    ))
    assert actions(report) == ["close", "resize", "open"]


def test_the_order_is_stable_within_each_group():
    """M483: a pass must be reproducible from its own record.

    Instruments keep their relative order inside each group, so reading the
    order log twice tells the same story. An unstable sort would make two
    identical passes place in different sequences.
    """
    report = BridgeReport(intents=(
        intent("DDD", "open"), intent("AAA", "open"),
        intent("CCC", "close"), intent("BBB", "close"),
    ))
    assert [i.ticker for i in report.actionable] == ["CCC", "BBB", "DDD", "AAA"], (
        "the sort reordered instruments within a group"
    )


def test_refusals_and_no_ops_are_never_placed():
    """M482: sorting must not smuggle non-acting intents into the queue.

    `refused` and `none` are decisions NOT to act. A sort that widened the
    filter would send a refusal to the broker, which is the one thing this
    layer must never do.
    """
    report = BridgeReport(intents=(
        intent("AAA", "open"), intent("BBB", "refused"),
        intent("CCC", "none"), intent("DDD", "close"),
    ))
    assert actions(report) == ["close", "open"]
    assert all(i.action in ("close", "resize", "open") for i in report.actionable)


def test_a_halted_report_places_nothing_whatever_the_order():
    """M484: the halt outranks the ordering.

    A halt means the account and the record disagree, so nothing is
    understood well enough to act on -- including a close. Reordering must
    not become a route around that.
    """
    report = BridgeReport(
        intents=(intent("AAA", "close"), intent("BBB", "open")),
        halted=True, halt_reason="reconciliation failed",
    )
    assert report.actionable == ()


def test_ordering_changes_nothing_about_what_is_decided():
    """The sequence moves; the set does not.

    If this ever removed or added an intent it would have stopped being an
    ordering change and become a strategy one, which would need the battery
    rather than a unit test.
    """
    built = (
        intent("AAA", "open"), intent("BBB", "close"),
        intent("CCC", "resize"), intent("DDD", "refused"),
    )
    report = BridgeReport(intents=built)

    assert sorted(i.ticker for i in report.actionable) == ["AAA", "BBB", "CCC"]
    assert len(report.actionable) == len([i for i in built if i.acts])
