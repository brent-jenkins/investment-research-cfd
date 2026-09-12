"""Did the system do what it said it would, today?

Every fault in the week to 2026-09-12 was found because the owner noticed
something or because somebody went digging: a last look that could not reach
its own pass mark, a concentration cap measuring a third of the book, a
nineteen-hour stall with the unit reporting `active`, a memory override that
outranked the unit files for a day.

None of them needed cleverness to catch. Each would have been obvious in a
plain daily statement of what the engine intended, what the account did,
what was refused and why, and what was held at the close -- which nothing
produced.

CLAUDE.md recorded that on 2026-09-10 as "worth more than any single fix
here and it is not built". This is it.

It verifies rather than monitors. `monitoring/` asks whether the machine is
alright; this asks whether the day's trading matches the day's intent, which
is a different question and the one nobody could answer.
"""

from cfdinvest.verify.daily import (
    Finding,
    DayReport,
    verify_day,
)

__all__ = ["Finding", "DayReport", "verify_day"]
