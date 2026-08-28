"""The simulation must never become the thing that places an order.

Its whole value is being a record nobody could have edited once the outcome
was known, and a book that sometimes trades is not that.

These two checks lived in the IG test files, because IG was the first broker
this project could reach and the isolation was written alongside it. They
were never about IG. When the IG integration was removed on 2026-08-28 --
measured non-viable at this account size, then superseded by Capital -- they
moved here, which is where they should always have been: they describe the
*simulation*, and they must outlive every venue the project ever talks to.

M108 the book importing a broker
M114 the book importing a path that can deal
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Every module that makes up the shadow book's decision and record. A new
#: one belongs here the day it is written, not the day it acquires an import
#: it should not have.
SHADOW_MODULES = ("papertrade.py", "papertrade_run.py", "capital_venue.py")


def _imports_of(name: str) -> list[str]:
    tree = ast.parse((Path("cfdinvest/advisor") / name).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def test_the_shadow_book_does_not_import_a_broker():
    """M108: the simulation acquiring a way to reach a venue.

    Asserted against the module tree rather than a run, because the failure
    would arrive as a new import and not as a bad number.
    """
    for name in SHADOW_MODULES:
        imported = _imports_of(name)
        assert not any("broker" in module for module in imported), (
            f"{name} imports a broker: "
            f"{[m for m in imported if 'broker' in m]}"
        )


def test_the_shadow_book_does_not_import_dealing():
    """M114: the simulation acquiring a path that can place an order.

    Stricter than the import check above and deliberately so: the word must
    not appear in these files at all. It costs nothing -- a comment can say
    "the live order path" instead -- and it means the check cannot be
    defeated by an import spelled some other way.
    """
    for name in SHADOW_MODULES:
        source = (Path("cfdinvest/advisor") / name).read_text(encoding="utf-8")
        assert "dealing" not in source, f"{name} references dealing"


def test_the_book_is_told_what_it_may_not_work_out_for_itself():
    """The flag the book records is the decision, never the outcome.

    "Was the live order path armed when this was decided" is knowable at the
    moment of the decision and never changes. "Did the broker place it" is
    knowable only afterwards, and writing it would mean editing a row after
    the fact -- so it lives in the order store and the two are joined for
    reading.

    Guarded here because the tempting fix, the day somebody wants the two in
    one place, is to add the field to the book.
    """
    from cfdinvest.advisor import papertrade

    fields = {f.name for f in papertrade.PaperTrade.__dataclass_fields__.values()}
    assert "live_armed" in fields
    for forbidden in ("placed", "filled", "deal_id", "broker_result", "order_id"):
        assert not any(forbidden in field for field in fields), (
            f"the book records {forbidden!r}, which is the broker's answer "
            "and belongs in the order store"
        )
