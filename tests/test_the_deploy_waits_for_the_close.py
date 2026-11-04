"""A push must not deploy into an open session, and must not do it quietly.

"Do not deploy during a session" has been the rule since 2026-09-10, when
nine deploys cost roughly four dealing passes. Nothing enforced it. On
2026-09-14 two deploys landed at 19:25:00Z and 19:39:33Z -- thirty-five and
twenty-one minutes before the close, market open -- because
`webhook_listener.should_deploy` is "a push to main, and nothing else" and a
push reaches the box within seconds. Nobody chose that; it was simply what
the deployment did, and it was discovered only when a deliberate deploy
reported "Now at 4d52e3b (was 4d52e3b)" because the webhook had already been.

THREE PARTS, AND THE THIRD IS THE ONE THAT MAKES IT SAFE.

  the question   `cfdinvest-schedule market-open` answers in its EXIT CODE:
                 0 open, 1 closed, 3 could not tell. It calls
                 `scheduler.any_open`/`open_assets`, which the dealing pass
                 already asks before every pass -- a caller, not a second
                 opinion.

  the guard      `deploy.sh` refuses unless the answer is exactly 1. Open
                 refuses; no universe refuses; an unreadable calendar
                 refuses. UNKNOWN IS NOT CLOSED, because a guard that waves
                 the deploy through when it cannot tell is the inert gate
                 this project has shipped twice -- once so strict nothing
                 could pass and once so inert everything did.

  the visibility A guard alone trades "a deploy landed mid-session" for "the
                 box quietly runs last week's code", and the second is
                 worse. That is the 2026-09-12 MemoryHigh finding exactly:
                 "the repository, the tests and the deploy all described one
                 configuration and the machine ran another."

                 So the guard refuses AFTER the fetch. origin/main moves on
                 disk, HEAD does not, and `check_deploy_is_current` reports
                 the box as behind on the next monitoring pass. The
                 placement is load-bearing: refusing before the fetch would
                 leave the two revisions identical and the deferred deploy
                 invisible.

The override is deliberate and differently worded, the way live dealing
needs its own variable: `CFDINVEST_DEPLOY_INTO_OPEN_MARKET=1`. It exists
because the rule has always carried an exception -- the 2026-09-10 deploys
were right, and the reason given was that what was running was actively
placing wrong trades.

Mutations checked:

  M1760 the guard placed BEFORE the fetch, so a refusal is invisible
  M1761 exit code 3 (unknown) treated as closed, so the guard disables
        itself exactly when it cannot see
  M1762 the guard dropped from deploy.sh entirely.

        **This one SURVIVED first time and the test was wrong, not the
        mutation.** The check was `"market-open" in source`, and the
        mutation replaced the call with `true # market-open` -- the
        substring still present, in a comment. Same trap as
        `entered_bars_ago` passing against `entered_bars_ago_renamed`, and
        as the unit-file assertion that passed against a commented-out
        line. It now requires an uncommented line that invokes the command.
  M1763 the override removed, so an urgent fix cannot go out
  M1764 a box behind origin/main reported as healthy
  M1765 unreadable revisions reported as current rather than unchecked
  M1766 the guard silent when it PERMITS, so there is no evidence it ran

And one gap the SUITE caught rather than a mutation: `deploy` was added to
`monitoring.TYPES` without a matching entry in `aggregate.TYPE_LABELS`, so
`test_the_new_issue_type_is_registered_everywhere` failed. A type with no
label renders to a person as a bare token -- the failure that test was
written for after `livequotes` shipped exactly that way. It is registered as
"Deployed revision".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cfdinvest.monitoring import check_deploy_is_current
from cfdinvest.monitoring.resources import read_deployed_revisions

DEPLOY = Path(__file__).resolve().parents[1] / "deployment" / "deploy.sh"


# --------------------------------------------------------------------- #
# The guard, read off the script that has to implement it.
# --------------------------------------------------------------------- #
def test_the_guard_refuses_unless_the_market_is_positively_closed() -> None:
    """M1761, M1762: the guard gone, or unknown spent as closed.

    Anchored on `-ne 1` rather than on "is it open": the command has three
    answers and only one of them may proceed. A test asserting `-eq 0`
    would pass against a guard that deploys when the calendar is unreadable.
    """
    source = DEPLOY.read_text(encoding="utf-8")

    # Anchored on an UNCOMMENTED invocation, not on the substring.
    # `assert "market-open" in source` passed against a mutation that
    # replaced the call with `true # market-open` -- the same substring trap
    # that once let `entered_bars_ago` pass against `entered_bars_ago_
    # renamed`, and the same shape as a unit-file assertion that passed
    # against a commented-out line. The mutation found it; it was not
    # noticed by reading.
    invoked = [
        line for line in source.splitlines()
        if "cfdinvest-schedule" in line
        and "market-open" in line
        and not line.lstrip().startswith("#")
    ]
    assert invoked, (
        "deploy.sh never actually runs market-open -- the rule is "
        "unenforced again, however many times the words appear in comments"
    )
    assert '[[ "${session}" -ne 1 ]]' in source, (
        "the guard proceeds on something other than a positive 'closed'. "
        "Exit 3 means the calendar could not be read, and treating that as "
        "closed disables the guard exactly when it cannot see"
    )


def test_the_guard_runs_after_the_fetch_so_a_refusal_is_visible() -> None:
    """M1760: the placement, which is the whole safety argument.

    Before the fetch, HEAD and origin/main stay equal and a deferred deploy
    is invisible -- the guard would then be trading a known problem for a
    silent one.
    """
    source = DEPLOY.read_text(encoding="utf-8")

    fetch = source.index("git fetch --quiet origin main")
    guard = source.index("market-open")
    reset = source.index("git reset --hard --quiet origin/main")

    assert fetch < guard < reset, (
        "the session guard must sit between the fetch and the reset: after "
        "it, so origin/main has moved and the box is measurably behind; "
        "before the reset, so nothing is actually deployed"
    )


def test_an_urgent_fix_can_still_go_out() -> None:
    """M1763: the override removed.

    The rule has always had an exception. On 2026-09-10 nine deploys went
    out mid-session and this project recorded that as correct, because what
    was running was placing wrong trades. A guard with no way past it would
    have prevented the one deploy that mattered.
    """
    source = DEPLOY.read_text(encoding="utf-8")

    assert "CFDINVEST_DEPLOY_INTO_OPEN_MARKET" in source, (
        "there is no way to deploy during a session, so an actively "
        "misbehaving account cannot be fixed until the close"
    )
    assert 'if [[ -z "${CFDINVEST_DEPLOY_INTO_OPEN_MARKET:-}" ]]' in source, (
        "the override is named but not consulted"
    )


def test_the_guard_leaves_a_trace_when_it_permits() -> None:
    """M1766: silent on the way through.

    The first deploy after this guard went in logged nothing about it --
    the refusal path speaks and the permit path did not -- so there was no
    evidence the guard had run at all. A gate that is silent when it
    permits cannot be distinguished from one that has stopped running,
    which is this project's oldest failure and the reason the completed-bar
    trim shipped inert twice in one afternoon while every test passed.
    """
    source = DEPLOY.read_text(encoding="utf-8")

    permits = [
        line for line in source.splitlines()
        if "Session guard" in line
        and "closed" in line
        and not line.lstrip().startswith("#")
    ]
    assert permits, (
        "the guard says nothing when it lets a deploy through, so a guard "
        "that stopped running would look exactly like one that ran"
    )


def test_the_script_still_parses() -> None:
    """A guard that breaks the shell blocks every deploy, urgent or not."""
    done = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------- #
# The question, answered as an exit code.
# --------------------------------------------------------------------- #
def test_market_open_reports_three_distinct_answers() -> None:
    """Open, closed and unknown must not collapse into two.

    The constants are asserted as distinct rather than for their values --
    "test the property, not the constant" -- but 1 is pinned, because
    deploy.sh branches on it literally and a silent renumbering would
    invert the guard.
    """
    from cfdinvest.schedule.app import MARKET_CLOSED, MARKET_OPEN, MARKET_UNKNOWN

    assert len({MARKET_OPEN, MARKET_CLOSED, MARKET_UNKNOWN}) == 3
    assert MARKET_CLOSED == 1, (
        "deploy.sh proceeds only on exit 1; changing this without changing "
        "the guard inverts it"
    )


def test_market_open_is_registered_as_a_command() -> None:
    """A command the deploy calls must exist, or every deploy refuses."""
    done = subprocess.run(
        [sys.executable, "-m", "cfdinvest.schedule.app", "market-open"],
        capture_output=True, text=True, timeout=180,
    )
    assert done.returncode in (0, 1, 3), (
        f"market-open returned {done.returncode}, which deploy.sh cannot "
        f"interpret. stderr: {done.stderr[-400:]}"
    )


# --------------------------------------------------------------------- #
# The visibility half.
# --------------------------------------------------------------------- #
def test_a_box_behind_the_repository_is_reported() -> None:
    """M1764: a deferred deploy that nobody can see.

    Without this the guard is a downgrade: it swaps a deploy landing in a
    session for a machine running code nobody chose, which is the failure
    this project has already paid for once.
    """
    issues = check_deploy_is_current(
        {"head": "a" * 40, "origin": "b" * 40, "behind": 3}
    )

    assert len(issues) == 1
    assert issues[0].type == "deploy"
    assert issues[0].severity == "warning", (
        "critical would cry on every push, in the minutes before the deploy "
        "lands; the point is visibility, not alarm"
    )
    assert "3 commit(s) behind" in issues[0].message


def test_a_current_box_says_nothing() -> None:
    """The quiet case, which must stay quiet or the check gets ignored."""
    same = "c" * 40
    assert check_deploy_is_current({"head": same, "origin": same, "behind": 0}) == []


def test_unreadable_revisions_are_unchecked_rather_than_current(tmp_path) -> None:
    """M1765: absence read as health.

    A box whose revisions cannot be read is not thereby up to date, and
    "not measured" and "measured and fine" are different statements.
    """
    issues = check_deploy_is_current(None)
    assert len(issues) == 1
    assert "could not be checked" in issues[0].message.lower()

    # And the reader agrees: a directory that is not a checkout reads empty
    # rather than inventing a revision.
    assert read_deployed_revisions(tmp_path) == {}


def test_the_reader_does_not_reach_the_network() -> None:
    """A health check that fetches fails whenever the network does.

    `deploy.sh` has already fetched by the time a refusal happens, so the
    comparison is answerable from disk. Asserted against the source because
    the failure guarded is a `git fetch` creeping in later.
    """
    from cfdinvest.monitoring import resources

    source = Path(resources.__file__).read_text(encoding="utf-8")
    body = source[source.index("def read_deployed_revisions"):]
    assert "fetch" not in body.split('"""')[2], (
        "read_deployed_revisions reaches the network, so the box reports as "
        "unknowable whenever the network is down"
    )
