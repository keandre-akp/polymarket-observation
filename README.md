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
├── fred.py        FRED daily pull → comparables           (week 2)
├── fedwatch.py    CME FedWatch Playwright scraper         (week 2)
└── notion_sync.py SQLite rollup → Notion DB               (week 2)

config/
├── markets.yml    Tracked Polymarket events
└── events.yml     FOMC / NFP / CPI release calendar

data/
└── observations.db  The SQLite tape (committed to repo)

.github/workflows/
└── poll-polymarket-hourly.yml  Hourly cron on GitHub Actions
```

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

## Discipline boundary

If a change in this repo starts computing edge, ranking spreads, or filtering for "big" mispricings — that's phase 3 territory. Stop and reconsider.
