"""SQLite schema and helpers for the Polymarket Observation tape.

Four tables:
    markets           one row per tracked sub-market (each event has many)
    price_snapshots   the tape: one row per poll per market
    events            scheduled FOMC / NFP / CPI / etc releases
    comparables       non-Polymarket data (FRED, FedWatch, Odds API)

The DB file lives at data/observations.db and is committed to the repo
for the 60-day observation window. Migrate to Turso ~week 4.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_REPO_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "observations.db"


def default_db_path() -> Path:
    """Where the SQLite tape lives. Override with $OBSERVATIONS_DB_PATH for tests."""
    return Path(os.environ.get("OBSERVATIONS_DB_PATH") or _REPO_DEFAULT_DB)


# Back-compat alias for code that imports the constant. Evaluated at import,
# so prefer default_db_path() in new code paths that need env-var override.
DEFAULT_DB_PATH = default_db_path()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS markets (
    market_id          TEXT    PRIMARY KEY,            -- Polymarket condition_id
    event_slug         TEXT    NOT NULL,               -- groups sub-markets by event
    event_name         TEXT    NOT NULL,               -- human name from config
    category           TEXT    NOT NULL,               -- Fed / Economic / Election
    question           TEXT    NOT NULL,               -- the specific YES/NO question
    outcome_name       TEXT,                           -- "No change", "25 bp cut", etc
    yes_token_id       TEXT    NOT NULL,               -- CLOB token id for YES
    no_token_id        TEXT,                           -- CLOB token id for NO
    polymarket_url     TEXT,
    resolution_date    TEXT,                           -- ISO date
    tracked_from       TEXT    NOT NULL,               -- ISO datetime UTC
    active             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id          TEXT    NOT NULL,
    timestamp          TEXT    NOT NULL,               -- ISO datetime UTC
    yes_price          REAL,                           -- midpoint, 0..1
    no_price           REAL,                           -- midpoint, 0..1
    yes_bid            REAL,
    yes_ask            REAL,
    volume_24h         REAL,
    mode               TEXT    NOT NULL DEFAULT 'routine',  -- routine / pre_event / post_event
    raw_response       TEXT,                           -- raw JSON for debugging
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_market_time
    ON price_snapshots(market_id, timestamp);

CREATE TABLE IF NOT EXISTS events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type                    TEXT NOT NULL,             -- FOMC / NFP / CPI / etc
    name                    TEXT NOT NULL,
    scheduled_datetime      TEXT NOT NULL,             -- ISO datetime UTC
    actual_release_datetime TEXT,
    notes                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_scheduled
    ON events(scheduled_datetime);

CREATE TABLE IF NOT EXISTS comparables (
    comp_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT    NOT NULL,                       -- cme_fedwatch / fred / odds_api
    series_id  TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL,                       -- ISO datetime UTC
    value      REAL    NOT NULL,
    notes      TEXT
);

CREATE INDEX IF NOT EXISTS idx_comparables_lookup
    ON comparables(source, series_id, timestamp);
"""


def utcnow_iso() -> str:
    """Current UTC time as an ISO 8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with row_factory=Row and foreign keys ON."""
    db_path = Path(db_path) if db_path else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create tables and indexes if they don't already exist."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


# ---------- markets ----------

def upsert_market(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    event_slug: str,
    event_name: str,
    category: str,
    question: str,
    outcome_name: str | None,
    yes_token_id: str,
    no_token_id: str | None,
    polymarket_url: str | None,
    resolution_date: str | None,
) -> None:
    """Insert a market, or update its mutable fields if it already exists.

    Idempotent: safe to call on every poller startup.
    """
    conn.execute(
        """
        INSERT INTO markets (
            market_id, event_slug, event_name, category, question,
            outcome_name, yes_token_id, no_token_id, polymarket_url,
            resolution_date, tracked_from, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(market_id) DO UPDATE SET
            event_name = excluded.event_name,
            category = excluded.category,
            question = excluded.question,
            outcome_name = excluded.outcome_name,
            yes_token_id = excluded.yes_token_id,
            no_token_id = excluded.no_token_id,
            polymarket_url = excluded.polymarket_url,
            resolution_date = excluded.resolution_date
        """,
        (
            market_id, event_slug, event_name, category, question,
            outcome_name, yes_token_id, no_token_id, polymarket_url,
            resolution_date, utcnow_iso(),
        ),
    )


def get_active_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM markets WHERE active = 1"))


def set_market_active(conn: sqlite3.Connection, market_id: str, active: bool) -> None:
    conn.execute(
        "UPDATE markets SET active = ? WHERE market_id = ?",
        (1 if active else 0, market_id),
    )


# ---------- price snapshots ----------

def save_price_snapshot(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    yes_price: float | None,
    no_price: float | None = None,
    yes_bid: float | None = None,
    yes_ask: float | None = None,
    volume_24h: float | None = None,
    mode: str = "routine",
    raw_response: str | None = None,
    timestamp: str | None = None,
) -> int:
    """Insert a price snapshot row. Returns the new snapshot_id."""
    cur = conn.execute(
        """
        INSERT INTO price_snapshots (
            market_id, timestamp, yes_price, no_price,
            yes_bid, yes_ask, volume_24h, mode, raw_response
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            timestamp or utcnow_iso(),
            yes_price, no_price,
            yes_bid, yes_ask,
            volume_24h, mode, raw_response,
        ),
    )
    return cur.lastrowid


# ---------- events ----------

def upsert_event(
    conn: sqlite3.Connection,
    *,
    type_: str,
    name: str,
    scheduled_datetime: str,
    notes: str | None = None,
) -> None:
    """Insert an event if no row with the same (type, name, scheduled_datetime) exists."""
    existing = conn.execute(
        "SELECT event_id FROM events WHERE type = ? AND name = ? AND scheduled_datetime = ?",
        (type_, name, scheduled_datetime),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO events (type, name, scheduled_datetime, notes) VALUES (?, ?, ?, ?)",
            (type_, name, scheduled_datetime, notes),
        )


def get_upcoming_events(
    conn: sqlite3.Connection,
    *,
    within_hours: int = 24,
) -> list[sqlite3.Row]:
    """Events whose scheduled_datetime is within `within_hours` of now (past or future)."""
    return list(conn.execute(
        """
        SELECT * FROM events
        WHERE scheduled_datetime BETWEEN
            datetime('now', ?) AND datetime('now', ?)
        ORDER BY scheduled_datetime ASC
        """,
        (f'-{within_hours} hours', f'+{within_hours} hours'),
    ))


# ---------- comparables ----------

def save_comparable(
    conn: sqlite3.Connection,
    *,
    source: str,
    series_id: str,
    value: float,
    timestamp: str | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO comparables (source, series_id, timestamp, value, notes) VALUES (?, ?, ?, ?, ?)",
        (source, series_id, timestamp or utcnow_iso(), value, notes),
    )
    return cur.lastrowid


# ---------- CLI: `python -m src.db init` ----------

def _main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db()
        print(f"Initialized DB at {DEFAULT_DB_PATH}")
    else:
        print("usage: python -m src.db init")
        sys.exit(1)


if __name__ == "__main__":
    _main()
