"""Confirming what actually happened, as against what was recommended.

The engine sizes a position, the analytics score it and the dashboard shows
it — all on the assumption that the recommendation was followed exactly. A
confirmation is where that assumption is replaced by a fact: the fill you got,
the quantity you took, or a note that you looked and passed.

::

    cfdinvest-confirm open SPX500 --price 4434.8 --quantity 0.684
    cfdinvest-confirm close NAS100 --price 18923.4
    cfdinvest-confirm list

```python
from cfdinvest.confirm import Confirmation, write_confirmation

write_confirmation(
    Confirmation.of("SPX500", "open", direction="long",
                    quantity=0.684, price=4434.8),
    config,
)
```

Also a page in the dashboard: **Confirm Trades**.

The storage lives in :mod:`cfdinvest.persist.confirm` with the rest of the
persistence layer; this package is the interfaces onto it — the command line
and the mail-reply parser.

**Nothing here places a trade.** It records that you did.
"""

from __future__ import annotations

from cfdinvest.confirm.reply import (
    ConfirmationCommand,
    parse_confirmation_email,
    parse_confirmation_line,
)
from cfdinvest.persist.confirm import (
    ACTIONS,
    Confirmation,
    confirmed_positions,
    deviations,
    list_confirmations,
    load_confirmations,
    load_latest_confirmation,
    merge_simulated_and_confirmed_positions,
    to_frame,
    write_confirmation,
)

__all__ = [
    "ACTIONS",
    "Confirmation",
    "write_confirmation",
    "load_confirmations",
    "load_latest_confirmation",
    "list_confirmations",
    "confirmed_positions",
    "merge_simulated_and_confirmed_positions",
    "deviations",
    "to_frame",
    "ConfirmationCommand",
    "parse_confirmation_email",
    "parse_confirmation_line",
]
