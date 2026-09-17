# AKP Polymarket Observation

Observation-only data ingestion for Polymarket prediction markets. Third system in the AKP stack, after the Momentum Engine and NBA tracker.

## What this is

A small Python pipeline that polls Polymarket (and eventually FRED, CME FedWatch, The Odds API) on a schedule, stores raw observations in SQLite, and writes daily rollups to a Notion database for human review.

**This system stores objective market data only.** It does NOT compute edge, generate signals, rank mispricings, or suggest trades. Those are subjective judgment calls that go in the user-filled columns of the Notion log.

The phase 1 goal is 30–60 days of clean data to validate whether independent reads on Polymarket produce edge. Zero money in Polymarket during the observation window.

## Architecture

```
src/
├── db.py          SQLite schema + helpers
├── poller.py      Polymarket Gamma + CLOB → price_snapshots
├── config.py      Loader for config/comparables.yml
├── fred.py        FRED daily pull → comparables
├── fedwatch.py    CME FedWatch Playwright scraper → comparables
└── notion_sync.py SQLite rollup → Notion DB

config/
├── markets.yml      Tracked Polymarket events
├── events.yml       FOMC / NFP / CPI release calendar
└── comparables.yml  FRED series, FedWatch scraping, Notion sync

data/
├── observations.db  The SQLite tape (committed to repo)
└── fedwatch_raw/    Raw HTML snapshots (gitignored; CI artifacts instead)

scripts/
└── commit_tape.sh   Shared "commit observations.db back" step

.github/workflows/
├── poll-polymarket-hourly.yml  Hourly Polymarket poll
├── fred-daily.yml              22:30 UTC weekdays
├── fedwatch-daily.yml          22:45 UTC weekdays
├── notion-sync-daily.yml       23:15 UTC weekdays
└── tests.yml                   pytest on push / PR
```

All four data workflows share the `observations-db` concurrency group so they
never race each other for the committed SQLite file.

## Storage

SQLite database lives at `data/observations.db` and is **committed back to the repo** on every poll. This is intentionally simple for the 60-day observation window. Migrate to Turso (hosted libSQL) around week 4.

## Data model

Four SQLite tables:
- `markets` — one row per tracked sub-market (each event has multiple sub-markets)
- `price_snapshots` — the tape; one row per poll per market
- `events` — scheduled releases (FOMC / NFP / CPI dates)
- `comparables` — non-Polymarket data points (FRED series, FedWatch probabilities)

## Notion log

Daily rollups land in the Observation Log database (sub-page of the 🔮 Polymarket Observation Log in Notion). System fills objective fields; user manually fills:
- My Independent Read (probability at entry)
- Edge Call (did the read identify mispricing)
- Hypothetical P&L (what $25 stake would have returned)
- Notes

## Week 2: comparables + Notion

### Secrets

`FRED_API_KEY` (<https://fred.stlouisfed.org/docs/api/api_key.html>) and
`NOTION_API_KEY` (<https://www.notion.so/my-integrations>), both under
**Settings → Secrets and variables → Actions**.

A Notion integration cannot see a database until you share it: open the
Observation Log → `···` → **Connections** → add the integration. Without that
every sync returns `object_not_found` rather than a permission error. Check it
with `python -m src.notion_sync --verify-schema`.

### Running

```bash
python -m src.fred                    # trailing 14-day window, idempotent
python -m src.fred --backfill         # ~2 years, one time
python -m src.fedwatch                # scrape + snapshot + parse
python -m src.fedwatch --dump-tables  # diagnose a failed parse
python -m src.notion_sync --dry-run   # print payloads, write nothing
python -m pytest -q                   # 60 tests, all offline
```

### The FedWatch scraper

Three things about the live tool, each confirmed in a browser on 2026-08-05 and
each of which breaks a naive scraper:

**The QuikStrike URL cannot be hit directly.** It runs a referrer check and
answers with `Access to QuikStrike has been denied - unexpected null referrer`.
The scraper loads the cmegroup.com page and works inside the iframe.

**The iframe is below the fold and lazy-loaded** — it never starts fetching in a
viewport that stays at the top, so the scraper scrolls first.

**The default view is the transpose of the expected grid.** It shows one meeting
at a time, with rate ranges as ROW labels and time offsets (`NOW`, `1 DAY`,
`1 WEEK`, `1 MONTH`) as columns. So the scraper walks the meeting tab strip, and
the parser tries three shapes: the wide meeting×range grid, the tall
range×time-offset grid taking `NOW`, and the `EASE` / `NO CHANGE` / `HIKE`
summary. No CSS selectors are pinned — QuikStrike's element ids are generated
and move between deploys.

The tool publishes EASE / NO CHANGE / HIKE itself, so the module also collapses
the bucket grid independently and compares. Published wins; a gap over half a
point is logged and recorded in the row's `notes` rather than averaged away.

Raw HTML is written to `data/fedwatch_raw/` *before* any parsing, so a layout
change costs a parser fix, not a lost day. `--reparse-all` rebuilds from
snapshots afterwards.

> CME publishes an official FedWatch REST API through `dataservices.cmegroup.com`.
> If access comes through, delete `src/fedwatch.py`.

### How a sub-market gets its comparable

No manual link table — the tape already carries enough. Two shapes qualify:

| Market shape | Example | Comparable |
| --- | --- | --- |
| resolution_date is an FOMC date | "No change", 2026-09-16 | `FOMC:2026-09-16:HOLD` |
| outcome is a rate level | "3.75%", end of 2026 | `FOMC:2026-12-09:350-375` |

Everything else gets none. That is deliberate: *"Will 3 Fed rate cuts happen in
2026?"* contains the word "cut", but it resolves on 2026-12-31 — not a meeting —
and FedWatch publishes no cuts-per-year series. The FOMC dates come from
`config/events.yml`, so adding 2027 meetings there is all that is needed to
extend the mapping.

On the current tape that maps 18 of 33 sub-markets; the 13 cuts-per-year
sub-markets are the intentional gap.

### Idempotency

`save_comparable` in `src/db.py` is a plain INSERT, which is right for
`price_snapshots` — every poll is a distinct observation. It is not right for a
FRED print: DFF for a given day is one fact. So `src/fred.py` and
`src/fedwatch.py` check `(source, series_id, timestamp)` before writing and
update in place on a revision. Both stamp at midnight UTC of the observation
date, so re-running a job never appends a second row for the same day.

## Week 4: forecasts, scoring, divergence, alarms (Sep 17, 2026)

Four modules and two workflow changes. All offline-tested (`tests/test_week4.py`).

| Module | Job | What |
| --- | --- | --- |
| `src/forecasts.py` | `resolve-markets` (hourly) | Timestamped, append-only record of every `My Independent Read`. First capture wins; edits become revisions; **no backdating, no forecasts after resolution**. `log` subcommand for benchmark forecasters. |
| `src/brier.py` | `resolve-markets` (hourly) | Brier for each revision-0 forecast vs. **the market price at the moment it was logged** (fair) and vs. close (reference only, for P&L). Writes `scores`. |
| `src/divergence.py` | `fedwatch-daily` | Appends `market_p - comparable_p` per mapped market per run. Records only — see the boundary below. |
| `src/health.py` | `health` (every 2h) | Tape staleness (>6h) and forecast-due (tracked market resolving within 48h with no forecast) checks. A failure opens/updates a GitHub issue labelled `health`; recovery closes it. |

**Safety fix, `src/notion_sync.py`:** the sync now reads each Notion page before updating it and never overwrites `Opening Probability`, `Open Date`, `Closing Probability` or `Actual Outcome` once they hold a value. Without this, the first live sync would have replaced the hand-entered Sep 9 opening probabilities with Sep 17 tape prices and flipped three hand-logged YES/NO outcomes back to "Unresolved". Tested in `tests/test_week4.py`.

Also: `poller.discover` now retires sub-markets whose event is `active: false` (they were being polled forever), and the hourly poll has a second cron at :30 because GitHub was delivering the single hourly trigger every 3–6h.

```bash
python -m src.forecasts capture --dry-run      # what would be recorded from Notion
python -m src.forecasts log --market <id> --forecaster claude --p 0.63
python -m src.brier --report
python -m src.divergence --report
python -m src.health
```

## Discipline boundary

If a change in this repo starts computing edge, ranking spreads, or filtering for "big" mispricings — that's phase 3 territory. Stop and reconsider.

**Amendment A1 (Sep 17, 2026):** logging a divergence *series* (`src/divergence.py`) is record-keeping and sits inside the boundary — provided it is never sorted, thresholded, filtered, or surfaced as a signal. The module prints in tape order on purpose; a test enforces it. Scoring a forecast after the fact (`src/brier.py`) is likewise measurement, not generation: it never produces a probability, only grades one.
