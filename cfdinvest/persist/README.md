# `persist/` — one run store, and what it promises

## The failure this prevents

Every artefact this project produces is read back by something that was not
running when it was written: a dashboard three days later, an analytics
battery three months later, a person asking why a position was opened. Two
failures follow from that, and both have happened elsewhere in the codebase.

**A half-written file that reads as corrupt is worse than one that reads as
absent.** An unattended machine writing at 04:00 will eventually be
interrupted mid-write, and "cannot be read" propagates as a traceback out of
a web request while "not there" is a fact the page can state.

**Two stores mean two answers to "what was the last run".** The scheduler
already wrote `<data root>/runs/<run id>/`; persisting the remaining
artefacts into that same directory keeps one store. A parallel tree under a
different root would have given every consumer somewhere else to look, and
they would have disagreed the first time one of them failed.

## What is in here

`storage.py` — where a run lives and how it is found. Resolution order:
explicit `AppConfig`, then `CFD_DATA_ROOT`, then the platform default.

`schema.py` — what each artefact must carry. Data, not prose, so both the
writer and the readers can be checked against it.

`writers.py` / `readers.py` — the two halves, deliberately asymmetric (see
below).

`runmeta.py` — what ran, when, over what, and what went wrong. Written last,
so its presence means the run reached the end.

`accounts.py` — the registry. The one store here that is rewritten.

`retention.py` — keeping the run directory off the disk ceiling.

`scheduler_state.py`, and the per-domain stores: `risk.py`, `monitoring.py`,
`confirm.py`, `journal.py`, `insights.py`, `bias.py`, `assessments.py`,
`metastrategy.py`, `simulation.py`.

## The three rules writers keep

**Atomic.** Written to a temporary name in the same directory and renamed
into place. Same directory because a rename across filesystems is a copy,
and a copy is not atomic.

**Append-only.** A run directory is added to, never cleared. A rerun under
the same identifier overwrites individual artefacts but never removes the
others, so a retry cannot destroy the record of the attempt it was retrying.

**Never fatal.** A writer that fails logs and returns; the caller carries on.
Persistence exists to make a completed run inspectable, and a failure to
record a run must not also destroy it. Frames are validated against the
schema and a mismatch is *logged* rather than refused — losing a whole run's
record over one renamed column protects nothing.

## And what readers keep

**Pure, read-only, and never raising.** These feed a dashboard whose job
includes reporting that the system is broken.

**Absence and failure are different.** A run that never wrote a P&L frame is
not the same as one whose P&L frame cannot be read; only the second is a
fault. Frame readers return empty either way, and `artefact_status()`
separates them for a caller that needs to know. This is the house rule about
naming absence, in the one place where getting it wrong would make a broken
run look like a quiet one.

**Nothing is recomputed.** A reader returns what was written or nothing.
Recalculating a missing artefact would put numbers on the dashboard that no
run ever produced.

## Judgement calls

**Required columns are a floor, not a fence.** Extra columns pass through
untouched; a missing required one is refused. The pipeline grows columns —
stops added seven, currency added five — and a schema rejecting unknown ones
would make every such addition a breaking change.

**The version travels with the artefact.** Every JSON document carries
`schema_version`; every Parquet artefact has its version in the run's
`metadata.json`. That is what lets a reader tell "written by an older
version", which is recoverable, from "corrupt", which is not.

**The config snapshot is the point of the metadata.** Without it the
dashboard can only show today's defaults and admit it does not know what the
run used. It is redacted on the way in — a snapshot of settings is exactly
where an API key would otherwise be written to disk in plain text and then
rendered on a page.

**The account registry is rewritten, and it is the only one.** Everything
else records observations — a reading, a verdict, a fill — which describe
moments that do not come back. The registry is configuration: it says what
exists *now*, and a history of what it used to say would be a second source
of truth about which accounts are legal. A corrupt registry falls back to
the built-in three rather than to nothing, because reading none would make
`live` unreachable.

**Account separation was built as a resolver, not a migration.**
`account=None` resolves to the layout that existed before it; a named
account inserts one path segment. Doing it that way made switching it on a
default change instead of moving every file on disk.

## Retention

Measured on the deployment on 2026-09-11, at a 50-name universe:

    a complete run                54MB
      analysis.parquet            46MB   85%
      signals.parquet            3.9MB
      trades.parquet             2.9MB
      positions / pnl / equity / regime   under 1MB together

So they are kept on different terms: the last few runs keep everything, every
run keeps its light artefacts for a long time, and beyond that the directory
goes.

**Two figures here were superseded and the second matters.** This said a run
was 31MB and 98% of it correlation rows; correlations were removed from the
analysis frame on 2026-09-11 (they are quadratic in the universe, and
stacking them is what wedged the daemon for a session), so the frame is now
prices, returns and volatility — still 85% of the run, because the universe
grew from 10 names to 50 at the same time.

And it said the light artefacts were "under half a megabyte together". They
are **8.0MB**, sixteen times that, on the same universe growth. At 21 runs in
today's session that is about 170MB a day of what retention deliberately
keeps, so the old "roughly 11GB a year becomes roughly 280MB" no longer
holds. The replacement figure is **not computed here**, because the right
number depends on a retention window nobody has re-chosen against these
sizes. 2.0GB is on disk across 30 runs as this is written.

Two refusals. **The newest run is never touched**, whatever the settings say
— it is what every reader defaults to, and a retention policy able to delete
the run the dashboard is about to open is a bug with a schedule. And **a run
is never partly pruned into an unreadable state**: heavy artefacts go as a
set, and the metadata saying the run existed stays.

## Mutations checked

Reintroducing each of these as a bug makes the suite fail: writing in place
rather than atomically; clearing a run directory on rerun; letting a writer
failure raise and take the run down; a corrupt document reading as a crash
rather than as absent; treating "never written" and "unreadable" as the same
status; rejecting an artefact for having extra columns; dropping the config
snapshot, or keeping it unredacted; the P&L components no longer summing to
the total across the round trip; a categorical regime index collapsing on
the Parquet round trip; NaN and infinity serialising as something other than
null; retention deleting the newest run; pruning taking an artefact that
cannot be rebuilt from what remains; and an unknown account name resolving
to the shared root instead of being refused.

## What it writes

`<data root>/runs/<run id>/` — the artefacts and `metadata.json`; the
per-domain stores under the same root; `accounts.json`; and the scheduler's
state file. With `CFD_ACCOUNT_MODE` set, all of it under one further path
segment. Nothing here writes to `raw/` — that belongs to `ingestion/`.
