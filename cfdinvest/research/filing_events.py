"""Company filings read by Claude, tested against the account's own window.

Pre-registered in CLAUDE.md on 2026-09-30, before any filing text was
fetched. The question is the one the live news trial asks, answered from
history: does Claude's reading of a company's own 8-K, accepted before
10:00 New York, call the move from 10:00 to the last bar's open that day?

EDGAR is used because it is the one text source with an honest history:
every filing is timestamped by the SEC when accepted, to the second, and
nothing later can move that stamp. Headlines have no such record before the
forward archive began on 2026-09-27.

**The window starts at 2025-08-01 because of hindsight.** Claude Haiku
4.5's training data runs to about mid-2025. A model that has read what
happened after a filing can let that colour its reading of it, and no
prompt can make it forget. Every filing in the window post-dates that.

The stages, each resumable, each appending to its own store under the data
root's ``research/edgar``:

    fetch    8-K texts from EDGAR (the press release, EX-99.1, where there
             is one; else the 8-K's own document), first 6,000 characters
    label    event and impact per filing, from ``claude-haiku-4-5``
    study    the pre-registered event study and its two conditional cuts
    engine   the account-model harness with the labels as news events,
             against the same run without them

    python -m cfdinvest.research.filing_events fetch|label|study|engine

Research only. Nothing here is read by the dealer.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: The first filing the test may use. See the module docstring on hindsight.
START = "2025-08-01"
#: The last day of the pre-registered window.
END = "2026-09-29"
#: Characters of filing text given to the model.
MAX_CHARS = 6000
#: Filings per labelling request. Filings are long; a small batch keeps one
#: failed request cheap.
BATCH_SIZE = 5
PROMPT_VERSION = "edgar-8k-2026-09-30.1"
#: Pause between EDGAR requests. The SEC limits clients to ten a second.
EDGAR_PAUSE = 0.15

SYSTEM = """You label SEC Form 8-K filings for a research record.

Each item gives a stock ticker, the company's name and the opening text of
one 8-K filing the company made (usually its press release). From the text
alone, decide:

event: the single best fit.
  rating_up / rating_down      not used for filings; choose another
  guidance_up / guidance_down  the company raises or cuts its outlook
  earnings_beat / earnings_miss  results clearly stronger or weaker than
                                 the company's own guidance, the prior year,
                                 or what the text says was expected
  deal            merger, acquisition, divestiture, contract, partnership
  legal_regulatory  lawsuit, investigation, settlement, approval, regulation
  product         launch, recall, trial result, outage
  management      executive change, layoffs, buyback, dividend, financing
  other           routine filings (votes, bylaws, exhibits, debt terms) and
                  anything that does not describe a business event

impact: what the event means for the company: "good", "bad" or "neutral".
Judge the substance, not the upbeat language press releases always use --
record results that the text itself shows were mixed or weaker as "bad" or
"neutral", not "good". Use "neutral" for "other" and whenever unclear.

Answer for every item, using its id."""


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def _dir(config=None) -> Path:
    from cfdinvest.persist.storage import account_root

    path = Path(account_root(config, None)) / "research" / "edgar"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a malformed row in %s", path)
    return rows


def _append(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True) + "\n")
            written += 1
    return written


# --------------------------------------------------------------------------- #
# EDGAR
# --------------------------------------------------------------------------- #
class _Text(HTMLParser):
    """Visible text from an HTML filing, scripts and styles dropped."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def plain_text(document: str) -> str:
    """Readable text from an HTML or plain-text filing, whitespace collapsed."""
    if "<" in document and ">" in document:
        parser = _Text()
        parser.feed(document)
        document = "".join(parser.parts)
    document = html.unescape(document)
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", document)).strip()


def exhibits(index_page: str) -> dict[str, str]:
    """``{document type: file name}`` from a filing's EDGAR index page.

    The type column is the SEC's own classification, so the press release
    is found by what it IS (EX-99.1) rather than what the company named the
    file. Matching file names first found 67 press releases in 280 filings:
    NVIDIA files its results as ``q2fy27pr.htm``, and the model was shown a
    cover page with no numbers on it.
    """
    found: dict[str, str] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", index_page, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
        if len(cells) < 4:
            continue
        kind = re.sub(r"<[^>]+>|&nbsp;", "", cells[3]).strip().upper()
        name = re.sub(r"<[^>]+>|&nbsp;.*", "", cells[2], flags=re.S).strip()
        if kind and name and kind not in found:
            found[kind] = name
    return found


def choose_document(types: Mapping[str, str], primary: str) -> str:
    """The press release (EX-99.1, or a lone EX-99) if there is one, else the 8-K."""
    for kind in ("EX-99.1", "EX-99.01", "EX-99"):
        name = types.get(kind, "")
        if name.lower().endswith((".htm", ".html", ".txt")):
            return name
    return primary


_ITEM = re.compile(r"\bItem\s+\d\.\d{2}\b")


def body(text: str, *, is_exhibit: bool) -> str:
    """The part worth reading: an 8-K from its first Item, past the cover page."""
    if is_exhibit:
        return text
    match = _ITEM.search(text)
    return text[match.start():] if match else text


def _get(url: str) -> bytes:
    from cfdinvest.ingestion.filings import _get as edgar_get

    time.sleep(EDGAR_PAUSE)
    return edgar_get(url)


def _eight_ks(cik: str, since: str, until: str) -> list[dict]:
    """8-K filings in the window from EDGAR's submissions record."""
    root = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    blocks = [root.get("filings", {}).get("recent", {})]
    # Older pages, when the recent block does not reach back to ``since``.
    for page in root.get("filings", {}).get("files", []) or []:
        if str(page.get("filingTo", "")) >= since:
            blocks.append(json.loads(_get(f"https://data.sec.gov/submissions/{page['name']}")))
    out = []
    for block in blocks:
        forms = block.get("form", []) or []
        for i, form in enumerate(forms):
            accepted = str(block["acceptanceDateTime"][i])
            if form != "8-K" or not (since <= accepted[:10] <= until):
                continue
            out.append({
                "accession": block["accessionNumber"][i],
                "accepted": accepted,
                "items": str((block.get("items") or [""] * len(forms))[i] or ""),
                "primary": block["primaryDocument"][i],
            })
    return out


def fetch(tickers: Iterable[str], config=None, *, since=START, until=END) -> int:
    """Fetch the text of every 8-K in the window not already stored."""
    from cfdinvest.ingestion.filings import _cik_map

    store = _dir(config) / "texts.jsonl"
    have = {row["accession"] for row in _read(store)}
    ciks = _cik_map()
    written = 0
    for ticker in tickers:
        cik = ciks.get(str(ticker).upper())
        if not cik:
            logger.info("No CIK for %s; skipped", ticker)
            continue
        try:
            filings = _eight_ks(cik, since, until)
        except Exception as error:  # noqa: BLE001 - one company, not the run
            logger.warning("No submissions for %s: %s", ticker, error)
            continue
        rows = []
        for filing in filings:
            if filing["accession"] in have:
                continue
            folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{filing['accession'].replace('-', '')}"
            try:
                page = _get(f"{folder}/{filing['accession']}-index.htm").decode("utf-8", "replace")
                chosen = choose_document(exhibits(page), filing["primary"])
                text = body(plain_text(_get(f"{folder}/{chosen}").decode("utf-8", "replace")),
                            is_exhibit=chosen != filing["primary"])
            except Exception as error:  # noqa: BLE001
                logger.warning("Could not read %s %s: %s", ticker, filing["accession"], error)
                continue
            rows.append({**filing, "ticker": str(ticker), "document": chosen,
                         "text": text[:MAX_CHARS]})
        written += _append(store, rows)
        logger.info("%s: %d new filing(s)", ticker, len(rows))
    return written


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def _schema() -> dict:
    from cfdinvest.ingestion.headline_labels import EVENTS, IMPACT

    return {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "event": {"type": "string", "enum": [e for e in EVENTS
                                                     if e not in ("price_move", "commentary")]},
                "impact": {"type": "string", "enum": list(IMPACT)},
            },
            "required": ["id", "event", "impact"],
            "additionalProperties": False,
        }}},
        "required": ["labels"],
        "additionalProperties": False,
    }


def label(config=None, *, client=None, limit: int | None = None) -> dict:
    """Label every stored filing not yet labelled under ``PROMPT_VERSION``."""
    import anthropic

    from cfdinvest.ingestion.headline_labels import MODEL, PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK
    from cfdinvest.ingestion.news_rss import company_names

    client = client or anthropic.Anthropic()
    folder = _dir(config)
    done = {r["accession"] for r in _read(folder / "labels.jsonl")
            if r.get("prompt_version") == PROMPT_VERSION}
    todo = [r for r in _read(folder / "texts.jsonl")
            if r["accession"] not in done and r.get("text")][:limit]
    usage = {"input": 0, "output": 0, "labelled": 0, "asked": len(todo)}
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        content = "\n\n".join(
            json.dumps({"id": i, "ticker": row["ticker"],
                        "company": (company_names(row["ticker"], config=config) or [row["ticker"]])[0],
                        "text": row["text"]}, ensure_ascii=False)
            for i, row in enumerate(batch)
        )
        try:
            response = client.messages.create(
                model=MODEL, max_tokens=2048, system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": _schema()}},
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except anthropic.APIError as error:
            logger.warning("A labelling batch failed: %s", error)
            continue
        usage["input"] += response.usage.input_tokens
        usage["output"] += response.usage.output_tokens
        if response.stop_reason != "end_turn":
            logger.warning("A labelling batch stopped early (%s)", response.stop_reason)
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            answer = json.loads(text).get("labels") or []
        except json.JSONDecodeError:
            continue
        rows = []
        for entry in answer:
            index = entry.get("id")
            if isinstance(index, int) and 0 <= index < len(batch):
                filing = batch[index]
                rows.append({"accession": filing["accession"], "ticker": filing["ticker"],
                             "accepted": filing["accepted"], "model": MODEL,
                             "prompt_version": PROMPT_VERSION,
                             "event": entry["event"], "impact": entry["impact"]})
        usage["labelled"] += _append(folder / "labels.jsonl", rows)
    usage["cost_usd"] = round(usage["input"] / 1e6 * PRICE_IN_PER_MTOK
                              + usage["output"] / 1e6 * PRICE_OUT_PER_MTOK, 4)
    return usage


def events(config=None) -> pd.DataFrame:
    """The labelled filings that carry a side, as the engine's news events."""
    from cfdinvest.signals.news import events_from_labels

    rows = [{"key": r["accession"], "prompt_version": r["prompt_version"],
             "ticker": r["ticker"], "about": "company", "event": r["event"],
             "impact": r["impact"], "first_seen": r["accepted"]}
            for r in _read(_dir(config) / "labels.jsonl")]
    return events_from_labels(rows, prompt_version=PROMPT_VERSION)


# --------------------------------------------------------------------------- #
# The pre-registered study
# --------------------------------------------------------------------------- #
def _raw_bars(tickers, root: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for ticker in tickers:
        path = root / f"{ticker}.parquet"
        if path.is_file():
            frame = pd.read_parquet(path)
            frame.index = pd.to_datetime(frame.index, utc=True)
            out[ticker] = frame
    return out


def session_rows(bars: Mapping[str, pd.DataFrame], start=START, end=END) -> pd.DataFrame:
    """One row per name per session: prior close, 10:00 open, last-bar open, spreads."""
    rows = []
    for ticker, frame in bars.items():
        frame = frame.sort_index()
        prior_close = None
        for day in pd.date_range(start, end, freq="B", tz="UTC"):
            ny = pd.Timestamp(day.date()).tz_localize("America/New_York")
            ten = (ny + pd.Timedelta(hours=10)).tz_convert("UTC")
            last = (ny + pd.Timedelta(hours=15)).tz_convert("UTC")
            close_bar = (ny + pd.Timedelta(hours=15)).tz_convert("UTC")
            if ten in frame.index and last in frame.index:
                entry, exit_ = frame.loc[ten], frame.loc[last]
                rows.append({
                    "ticker": ticker, "day": day, "ten": ten,
                    "move": exit_["open"] / entry["open"] - 1,
                    "pre": (entry["open"] / prior_close - 1) if prior_close else np.nan,
                    "cost": (float(entry.get("spread_bp", np.nan))
                             + float(exit_.get("spread_bp", np.nan))) / 2 / 1e4,
                })
            if close_bar in frame.index:
                prior_close = float(frame.loc[close_bar, "close"])
    table = pd.DataFrame(rows)
    if table.empty:
        return table
    table = table.sort_values(["ticker", "day"])
    table["typical"] = (table.groupby("ticker")["move"]
                        .transform(lambda s: s.abs().rolling(60, min_periods=20).median().shift(1)))
    return table


def filing_sides(events_frame: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    """Net filing side per name-day, from filings accepted in the 24h before 10:00."""
    out = sessions.copy()
    out["side"] = 0
    if events_frame.empty or out.empty:
        return out
    grouped = {t: g for t, g in events_frame.groupby("ticker")}
    sides = []
    for row in out.itertuples():
        group = grouped.get(row.ticker)
        if group is None:
            sides.append(0)
            continue
        window = group[(group["first_seen"] > row.ten - pd.Timedelta(hours=24))
                       & (group["first_seen"] <= row.ten)]
        sides.append(int(np.sign(window["sign"].sum())))
    out["side"] = sides
    return out


def _summary(values: pd.DataFrame, column: str) -> dict:
    per_day = values.groupby("day")[column].mean()
    n = len(per_day)
    half = n // 2
    return {
        "name_days": int(len(values)), "days": int(n),
        "mean": float(per_day.mean()) if n else float("nan"),
        "t": float(per_day.mean() / (per_day.std(ddof=1) / np.sqrt(n))) if n > 2 else float("nan"),
        "h1": float(per_day.iloc[:half].mean()) if half else float("nan"),
        "h2": float(per_day.iloc[half:].mean()) if n - half else float("nan"),
    }


def study(table: pd.DataFrame, engine_direction: pd.DataFrame | None = None) -> dict:
    """The primary test and the two conditional cuts, exactly as registered."""
    hits = table[table["side"] != 0].copy()
    hits["gross"] = hits["side"] * hits["move"]
    hits["net"] = hits["gross"] - hits["cost"].fillna(hits["cost"].median())
    result = {"gross": _summary(hits, "gross"), "net": _summary(hits, "net")}

    typical = hits["typical"]
    priced = (np.sign(hits["pre"]) == hits["side"]) & (hits["pre"].abs() >= 0.5 * typical)
    known = hits["pre"].notna() & typical.notna()
    result["not_yet_priced"] = _summary(hits[known & ~priced], "net")
    result["already_priced"] = _summary(hits[known & priced], "net")

    if engine_direction is not None:
        def engine_side(row):
            stamp = row.ten - pd.Timedelta(hours=1)
            if stamp in engine_direction.index and row.ticker in engine_direction.columns:
                value = engine_direction.at[stamp, row.ticker]
                return {"long": 1, "short": -1}.get(str(value), 0)
            return 0
        hits["engine"] = [engine_side(r) for r in hits.itertuples()]
        result["with_trend"] = _summary(hits[hits["engine"] == hits["side"]], "net")
        result["against_trend"] = _summary(hits[hits["engine"] == -hits["side"]], "net")
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    from cfdinvest.config import load_config

    parser = argparse.ArgumentParser(prog="python -m cfdinvest.research.filing_events")
    parser.add_argument("stage", choices=("fetch", "label", "study", "engine"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    names = list(config.universe.candidates)

    if args.stage == "fetch":
        print("fetched", fetch(names, config))
    elif args.stage == "label":
        print(label(config, limit=args.limit))
    elif args.stage in ("study", "engine"):
        from cfdinvest.backtest.runner import run_pipeline
        from cfdinvest.repository import DataRepository
        from cfdinvest.research.harness import research_backtest

        repository = DataRepository(config)
        found = events(config)
        print(f"labelled filings carrying a side: {len(found)}")
        base = research_backtest(config, tickers=names, start="2025-06-01", end=END)
        plain = run_pipeline(repository, base)
        if args.stage == "study":
            root = Path(config.paths.raw_market_dir) / str(config.market.interval)
            table = filing_sides(found, session_rows(_raw_bars(names, root)))
            direction = plain.signals.direction
            direction.index = pd.to_datetime(direction.index, utc=True)
            print(json.dumps(study(table, direction), indent=2, default=str))
        else:
            import dataclasses

            from cfdinvest.research.harness import arm_metrics, membership_mask, screen_selections

            # The screen's weekly selection, built ONCE and given to both
            # arms, so the only difference between them is the news.
            closes = plain.analysis.close
            index = pd.DatetimeIndex(closes.index)
            dates = pd.date_range(index[0], index[-1], freq="W", tz=index.tz)
            universe = config.universe
            selections = screen_selections(
                repository, config, dates,
                cap=getattr(universe, "max_per_sector", None),
                target=int(getattr(universe, "max_target", 50) or 50),
            )
            gated = dataclasses.replace(
                research_backtest(config, tickers=list(closes.columns), start="2025-06-01", end=END,
                                  entry_gate=membership_mask(selections, index, closes.columns)),
            )
            without = arm_metrics(run_pipeline(repository, gated))
            with_news = arm_metrics(run_pipeline(repository, dataclasses.replace(gated, news_events=found)))
            print("without", without)
            print("with   ", with_news)
    return 0


if __name__ == "__main__":
    sys.exit(main())
