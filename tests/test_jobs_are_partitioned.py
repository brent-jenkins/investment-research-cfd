"""Every scheduled job is claimed by exactly one unit.

The scheduler was one resident loop running twelve jobs SEQUENTIALLY. On
2026-09-11 the pipeline wedged inside one of them and everything behind it
stopped for nineteen hours -- including the account's ability to CLOSE, with
a position waiting on a three-and-a-half-year backtest to finish before it
could be flattened at the bell.

Splitting them across units fixes that and introduces a new way to fail, one
that is silent in both directions:

  * a job claimed by NO unit never runs, and nothing says so -- the same
    shape as the monitor timer that sat disabled from 2026-08-26 through two
    stalls;
  * a job claimed by TWO units runs twice, which for an ingester is two
    writers against one store. `storage.write_parquet` renames a sibling
    .tmp into place, so a reader never sees a torn file -- but that
    guarantee is about a reader and a writer, NOT about two writers.

So the partition is asserted against the SHIPPED UNIT FILES rather than
against a list in the test, because a list in the test is a second copy that
drifts. Adding a job to `JOBS` without giving it an owner fails the build.

Mutations checked:

  M1660 a job dropped from every --only, so it never runs
  M1661 a job claimed by two units, so it runs twice
  M1662 a unit's --only naming a job that does not exist
  M1663 the dealer's job claimed by a scheduler unit as well
  M1664 --only dropped from a unit entirely, so it claims everything
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[1] / "deployment" / "systemd"

#: Units that run scheduler jobs. The dealer is deliberately absent: it runs
#: `trade-capital`, which is an EXTERNAL_JOB and not in JOBS at all, so
#: exactly one thing deals.
SCHEDULER_UNITS = (
    "cfdinvest.service",
    "cfdinvest-quotes.service",
    "cfdinvest-ingest.service",
    "cfdinvest-daily.service",
)


def claims(unit: str) -> list[str]:
    """The jobs a unit claims, read from its ExecStart --only.

    Tolerates the line continuation the units use, and ignores commented-out
    examples: a test that matched `# --only foo` would pass against a unit
    that claims nothing, which is this project's oldest failure.
    """
    body = (SYSTEMD / unit).read_text(encoding="utf-8")

    live = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    joined = live.replace("\\\n", " ")

    found = re.search(r"--only\s+([A-Za-z0-9_,\-]+)", joined)
    if not found:
        return []
    return [name for name in found.group(1).split(",") if name]


def test_every_unit_claims_something() -> None:
    """M1664: a unit with no --only runs every owed job, not its own."""
    for unit in SCHEDULER_UNITS:
        assert claims(unit), (
            f"{unit} declares no --only, so it runs EVERY owed job -- which "
            "is the single sequential loop again, wearing four unit files"
        )


def test_the_jobs_are_partitioned_exactly() -> None:
    """M1660 and M1661: nothing unclaimed, nothing claimed twice."""
    from cfdinvest.schedule import daemon

    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for unit in SCHEDULER_UNITS:
        for job in claims(unit):
            if job in seen:
                duplicated.append(f"{job} ({seen[job]} and {unit})")
            seen[job] = unit

    assert not duplicated, (
        "these jobs are claimed by more than one unit, so they run twice -- "
        f"two writers against one store: {duplicated}"
    )

    unclaimed = set(daemon.JOBS) - set(seen)
    assert not unclaimed, (
        f"these jobs have no owner and will never run: {sorted(unclaimed)}. "
        "A job added to JOBS must be given to a unit in the same change."
    )


def test_no_unit_claims_a_job_that_does_not_exist() -> None:
    """M1662: a renamed job leaves a unit claiming nothing, silently."""
    from cfdinvest.schedule import daemon

    known = set(daemon.JOBS)
    for unit in SCHEDULER_UNITS:
        unknown = set(claims(unit)) - known
        assert not unknown, (
            f"{unit} claims {sorted(unknown)}, which is not in JOBS -- the "
            "job was renamed or removed and the unit was not updated"
        )


def test_the_dealer_is_not_a_scheduler_unit() -> None:
    """M1663: exactly one thing deals, and it is not one of these.

    `trade-capital` is an EXTERNAL_JOB precisely so it cannot be scheduled
    here. A scheduler unit claiming it would mean two processes dealing on
    one account.
    """
    from cfdinvest.schedule import daemon

    for job in daemon.EXTERNAL_JOBS:
        for unit in SCHEDULER_UNITS:
            assert job not in claims(unit), (
                f"{unit} claims {job}, which is dealt by "
                "cfdinvest-dealer.service -- two processes would deal"
            )


@pytest.mark.parametrize("unit", SCHEDULER_UNITS)
def test_each_unit_exits(unit: str) -> None:
    """A unit that never exits is the resident loop, and the leak with it."""
    body = (SYSTEMD / unit).read_text(encoding="utf-8")

    found = re.search(r"^Type=(.*)$", body, re.M)
    assert found and found.group(1).strip() == "oneshot", f"{unit} is not oneshot"
    assert "--max-ticks" in body.replace("\\\n", " "), (
        f"{unit} does not bound its ticks, so Type=oneshot waits forever on "
        "a loop that never returns and the unit looks healthy"
    )


@pytest.mark.parametrize("unit", SCHEDULER_UNITS)
def test_each_unit_has_a_timer_that_starts_it(unit: str) -> None:
    """A one-shot nobody starts is a unit that stopped."""
    timer = SYSTEMD / unit.replace(".service", ".timer")
    assert timer.exists(), f"{unit} has no timer, so nothing runs it"

    body = timer.read_text(encoding="utf-8")
    found = re.search(r"^Unit=(.*)$", body, re.M)
    assert found and found.group(1).strip() == unit, (
        f"{timer.name} does not start {unit}"
    )
    assert re.search(r"^OnUnitInactiveSec=", body, re.M), (
        f"{timer.name} must fire relative to when the last pass FINISHED; "
        "OnUnitActiveSec measures from the start, so a pass slower than the "
        "interval is immediately due again"
    )
