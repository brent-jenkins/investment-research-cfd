"""The journal: what happened, and what was known when it did.

Almost every test here is about one property — **an entry is written from
what was known at the time and never rewritten**. A journal that regenerated
its narratives with the outcome in hand would teach "I was right to take it
because it won", and a good decision that lost is indistinguishable from a bad
one that won once the result is written over the reasoning.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.journal import (
    JournalEntry,
    build_entries,
    compose_entry,
    compose_outcome_entry,
    entry_key,
    filter_entries,
    group_by_day,
    narrate,
    regime_note,
    render_entry,
    render_journal,
    what_changed,
    write_journal,
)
from cfdinvest.persist import journal as store
from cfdinvest.persist.confirm import Confirmation

NOW = pd.Timestamp("2026-08-25 09:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def opened(ticker="SPX500", **changes) -> Confirmation:
    fields = dict(
        direction="long", quantity=0.68, price=4436.0,
        recommended_price=4434.8, recommended_quantity=0.7, timestamp=NOW,
    )
    fields.update(changes)
    return Confirmation.of(ticker, "open", **fields)


def entry(**changes) -> JournalEntry:
    fields = dict(timestamp=NOW, kind="trade", instrument="SPX500")
    fields.update(changes)
    return JournalEntry(**fields)


# --------------------------------------------------------------------------- #
# The narrative
# --------------------------------------------------------------------------- #
def test_an_open_reads_as_a_sentence():
    assert narrate(opened()) == (
        "Opened long SPX500, 0.6800 units at 4,436.0000."
    )


def test_the_plan_is_recorded_with_the_trade():
    """A year later, the levels are the thing you cannot reconstruct."""
    sentence = narrate(
        opened(), None, {"stop_level": 4390.0, "take_profit_level": 4520.0}
    )
    assert "stop 4,390.0000" in sentence
    assert "target 4,520.0000" in sentence


def test_a_close_reads_as_a_close():
    assert "Closed NAS100" in narrate(
        Confirmation("NAS100", "close", price=18_923.4, timestamp=NOW)
    )


def test_a_flip_names_both_legs():
    sentence = narrate(
        Confirmation.of("SPX500", "flip", direction="short", quantity=0.5,
                        price=4436.1, close_price=4434.8, timestamp=NOW)
    )
    assert "closing at 4,434.8000" in sentence
    assert "opening at 4,436.1000" in sentence


def test_a_trade_you_declined_is_still_an_entry():
    """The trades you did not take are half the record."""
    sentence = narrate(
        Confirmation("GLD", "ignore", notes="spread too wide", timestamp=NOW)
    )
    assert "did not take it" in sentence
    assert "spread too wide" in sentence


def test_the_narrative_passes_no_judgement():
    """It would be judging with less information than the reader will have."""
    sentence = narrate(opened())
    for word in ("good", "bad", "mistake", "should", "well done", "poor"):
        assert word not in sentence.lower()


def test_declining_is_recorded_as_considered_not_as_a_trade():
    result = compose_entry(
        Confirmation("GLD", "ignore", timestamp=NOW)
    )
    assert result.kind == "considered"


# --------------------------------------------------------------------------- #
# The three lenses
# --------------------------------------------------------------------------- #
def test_the_execution_note_reports_the_slippage_against_the_plan():
    result = compose_entry(opened())
    assert "worse than planned" in result.execution_note
    assert "97%" in result.execution_note


def test_a_clean_fill_says_so():
    result = compose_entry(
        opened(price=4434.8, quantity=0.7, recommended_quantity=0.7)
    )
    assert result.execution_note == "Filled as planned."


def test_an_absent_behavioural_layer_is_named_not_assumed_clean():
    result = compose_entry(opened())
    assert "No behavioural profile was available" in result.behavioural_note


def test_taking_a_trade_against_the_verdict_is_recorded():
    """The single most useful thing a journal can catch you doing."""
    result = compose_entry(opened(), assessment={"status": "UNSAFE"})
    assert "UNSAFE verdict standing" in result.behavioural_note
    assert result.verdict == "UNSAFE"


def test_the_three_notes_stay_separate():
    """Evidence about you and evidence about the market lead to different
    corrections; one blob invites attributing the first to the second."""
    result = compose_entry(opened(), regimes={"volatility_regime": "high"})
    assert result.execution_note
    assert result.regime_note
    assert result.behavioural_note
    assert result.regime_note not in result.behavioural_note


def test_the_regime_note_reads_in_english():
    assert regime_note(
        {"volatility_regime": "high", "combined_regime": "crisis"},
        ("volatility_regime",),
    ) == "Volatility high, combined crisis. Elevated: volatility."


def test_no_regime_is_named_rather_than_omitted():
    assert "No regime classification" in regime_note(None)


# --------------------------------------------------------------------------- #
# What changed
# --------------------------------------------------------------------------- #
def test_the_first_entry_has_nothing_to_compare_against():
    assert what_changed({"timestamp": NOW}, None) == (
        "First entry — nothing to compare against yet.",
    )


def test_a_regime_turn_is_the_thing_a_journal_can_see():
    """Invisible in today's snapshot and obvious in the difference."""
    changes = what_changed(
        {"timestamp": NOW, "regimes": {"volatility_regime": "high"}},
        entry(timestamp=NOW - pd.Timedelta("3D"),
              regimes={"volatility_regime": "low"}),
    )
    assert any("moved from low to high" in change for change in changes)


def test_a_verdict_change_is_noted():
    changes = what_changed(
        {"timestamp": NOW, "verdict": "UNSAFE"},
        entry(timestamp=NOW - pd.Timedelta("1D"), verdict="SAFE"),
    )
    assert any("SAFE to UNSAFE" in change for change in changes)


def test_the_gap_since_the_last_entry_is_recorded():
    changes = what_changed(
        {"timestamp": NOW}, entry(timestamp=NOW - pd.Timedelta("9D"))
    )
    assert any("9 day(s) since" in change for change in changes)


def test_a_quiet_period_says_so_rather_than_nothing():
    changes = what_changed(
        {"timestamp": NOW, "regimes": {"volatility_regime": "low"}},
        entry(timestamp=NOW, regimes={"volatility_regime": "low"},
              instrument="SPX500"),
    )
    assert changes == ("Nothing material has changed since the last entry.",)


# --------------------------------------------------------------------------- #
# The outcome is a separate entry
# --------------------------------------------------------------------------- #
def test_an_outcome_is_its_own_entry():
    """Never merged into the entry that opened the position."""
    result = compose_outcome_entry(
        "SPX500", realised_pnl=142.5, exit_price=4480.0, closed_at=NOW
    )
    assert result.kind == "outcome"
    assert "142.50" in result.narrative


def test_the_outcome_says_the_pnl_was_not_recomputed():
    result = compose_outcome_entry("SPX500", realised_pnl=142.5, closed_at=NOW)
    assert "not recomputed here" in result.narrative


def test_the_outcome_says_to_read_it_beside_the_decision():
    result = compose_outcome_entry("SPX500", realised_pnl=-80.0, closed_at=NOW)
    assert "beside the original entry rather than instead of it" in (
        result.behavioural_note
    )


def test_writing_an_outcome_does_not_touch_the_opening_entry(lake):
    """The property the whole layer turns on."""
    original = compose_entry(opened(), regimes={"volatility_regime": "low"})
    store.append_entry(original, lake)
    store.append_entry(
        compose_outcome_entry("SPX500", realised_pnl=200.0, closed_at=NOW), lake
    )

    entries = store.load_entries(lake)
    assert len(entries) == 2
    assert entries[0].narrative == original.narrative
    assert entries[0].kind == "trade" and entries[1].kind == "outcome"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_an_entry_round_trips(lake):
    store.append_entry(
        compose_entry(opened(), regimes={"volatility_regime": "high"}), lake
    )
    loaded = store.load_entries(lake)

    assert len(loaded) == 1
    assert loaded[0].instrument == "SPX500"
    assert loaded[0].regimes == {"volatility_regime": "high"}
    assert loaded[0].changed


def test_entries_append_rather_than_replace(lake):
    for day in range(3):
        store.append_entry(
            entry(timestamp=NOW + pd.Timedelta(days=day)), lake
        )
    assert len(store.load_entries(lake)) == 3


def test_a_truncated_line_loses_one_entry_not_the_journal(lake):
    """An append log's last line is what a crash takes."""
    store.append_entry(entry(narrative="kept"), lake)
    with store.journal_path(lake).open("a", encoding="utf-8") as handle:
        handle.write('{"truncated": ')

    loaded = store.load_entries(lake)
    assert len(loaded) == 1
    assert loaded[0].narrative == "kept"


def test_a_failed_write_returns_none_rather_than_raising(lake, monkeypatch):
    """The caller is usually finishing something more important."""
    monkeypatch.setattr(
        Path, "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert store.append_entry(entry(), lake) is None


def test_no_journal_is_an_empty_list(lake):
    assert store.load_entries(lake) == []
    assert store.load_journal_frame(lake).empty


def test_an_unknown_field_from_a_later_version_still_loads(lake):
    document = entry().to_document() | {"invented_later": True}
    path = store.journal_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    assert store.load_entries(lake)[0].instrument == "SPX500"


def test_an_invented_kind_is_refused():
    with pytest.raises(ValueError, match="kind must be one of"):
        JournalEntry(timestamp=NOW, kind="musing")


def test_the_frame_is_sorted_and_typed(lake):
    store.append_entry(entry(timestamp=NOW), lake)
    store.append_entry(entry(timestamp=NOW - pd.Timedelta("1D")), lake)

    frame = store.load_journal_frame(lake)
    assert frame["timestamp"].is_monotonic_increasing


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #
def test_every_confirmation_becomes_an_entry():
    entries = build_entries([opened("SPX500"), opened("GLD", price=160.0)])
    assert len(entries) == 2
    assert {e.instrument for e in entries} == {"SPX500", "GLD"}


def test_a_confirmation_already_journalled_is_skipped():
    """Otherwise the journal grows by the size of your history each run."""
    first = build_entries([opened()])
    again = build_entries([opened()], already={entry_key(first[0])})
    assert again == []


def test_entries_chain_their_comparisons():
    """Each new entry compares against the one before it, not against the
    same starting point."""
    entries = build_entries(
        [
            opened("SPX500", timestamp=NOW),
            opened("GLD", price=160.0, timestamp=NOW + pd.Timedelta("2D")),
        ]
    )
    assert "First entry" in entries[0].changed[0]
    assert any("SPX500 last time" in c for c in entries[1].changed)


def test_an_assessment_after_the_fill_is_not_attached():
    """A verdict reached after the trade is a different thought, and putting
    it in the entry would credit the decision with information it lacked."""
    assessments = pd.DataFrame(
        [
            {"instrument": "SPX500", "status": "SAFE",
             "timestamp": (NOW - pd.Timedelta("1h")).isoformat()},
            {"instrument": "SPX500", "status": "UNSAFE",
             "timestamp": (NOW + pd.Timedelta("1h")).isoformat()},
        ]
    )
    entries = build_entries([opened()], assessments=assessments)
    assert entries[0].verdict == "SAFE"


def test_the_writer_is_idempotent_over_a_real_lake(lake):
    from cfdinvest.persist.confirm import write_confirmation

    write_confirmation(opened(), lake)
    assert len(write_journal(lake)) == 1
    assert write_journal(lake) == []
    assert len(store.load_entries(lake)) == 1


def test_the_writer_survives_an_empty_lake(lake):
    assert write_journal(lake) == []


# --------------------------------------------------------------------------- #
# Reading back
# --------------------------------------------------------------------------- #
def test_filtering_by_instrument():
    entries = build_entries([opened("SPX500"), opened("GLD", price=160.0)])
    assert len(filter_entries(entries, instrument="gld")) == 1


def test_filtering_by_kind():
    entries = [entry(kind="trade"), entry(kind="outcome")]
    assert len(filter_entries(entries, kind="outcome")) == 1


def test_filtering_by_date():
    entries = [
        entry(timestamp=NOW - pd.Timedelta("40D")),
        entry(timestamp=NOW),
    ]
    assert len(filter_entries(entries, since=NOW - pd.Timedelta("7D"))) == 1


def test_filtering_returns_them_in_order():
    entries = [entry(timestamp=NOW), entry(timestamp=NOW - pd.Timedelta("1D"))]
    ordered = filter_entries(entries)
    assert ordered[0].timestamp < ordered[1].timestamp


def test_rendering_keeps_the_lenses_on_separate_lines():
    text = "\n".join(render_entry(compose_entry(
        opened(), regimes={"volatility_regime": "high"})))
    assert "Execution:" in text
    assert "Market:" in text
    assert "You:" in text


def test_rendering_an_empty_journal_says_so():
    assert render_journal([]) == "The journal is empty."


def test_rendering_puts_the_newest_first():
    text = render_journal(
        [entry(timestamp=NOW - pd.Timedelta("1D"), narrative="older"),
         entry(timestamp=NOW, narrative="newer")]
    )
    assert text.index("newer") < text.index("older")


def test_grouping_by_day():
    grouped = group_by_day(
        [entry(timestamp=NOW), entry(timestamp=NOW + pd.Timedelta("2h")),
         entry(timestamp=NOW - pd.Timedelta("2D"))]
    )
    assert len(grouped) == 2


# --------------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------------- #
def test_the_digest_carries_every_entry():
    from cfdinvest.journal.digest import build_journal_digest

    body = build_journal_digest(build_entries([opened()])).get_content()
    assert "Opened long SPX500" in body


def test_an_empty_digest_explains_what_empty_means():
    """"Nothing journalled" is not "no trades"."""
    from cfdinvest.journal.digest import build_journal_digest

    body = build_journal_digest([]).get_content()
    assert "not the same as no trades" in body


def test_the_digest_says_entries_are_never_rewritten():
    from cfdinvest.journal.digest import build_journal_digest

    body = build_journal_digest(build_entries([opened()])).get_content()
    assert "never rewritten" in body
    # The notice now says what the system *can* do -- deal on a demo
    # account -- rather than claiming it cannot trade, which stopped
    # being true on 2026-08-27. Asserted by meaning, not wording.
    assert "demo account" in body and "live account" in body


def test_the_digest_subject_counts_the_entries():
    from cfdinvest.journal.digest import build_journal_digest

    message = build_journal_digest(
        build_entries([opened("SPX500"), opened("GLD", price=160.0)])
    )
    assert "2 entries" in message["Subject"]


# --------------------------------------------------------------------------- #
# It recomputes nothing
# --------------------------------------------------------------------------- #
#: Importing any of these would let the journal reach a conclusion of its own,
#: and a second answer to a question another layer already owns.
FORBIDDEN = (
    "cfdinvest.analytics",
    "cfdinvest.backtest",
    "cfdinvest.livehelper.safety",
    "cfdinvest.livehelper.sizing_helper",
    "cfdinvest.execution",
    "requests",
    "urllib",
)

MODULES = sorted(Path("cfdinvest/journal").glob("*.py"))


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_journal_computes_nothing(module):
    """The spec's rule, made structural: no recomputation, no safety logic,
    no sizing logic."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    offending = sorted(
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)
    )
    assert not offending, f"{module.name} imports {offending}"


def test_the_journal_reads_the_regime_through_the_risk_layer():
    """One opinion about what the market is doing, not a second."""
    source = Path("cfdinvest/journal/writer.py").read_text(encoding="utf-8")
    assert "compute_regime_risk" in source
