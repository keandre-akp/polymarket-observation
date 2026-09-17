"""Divergence log: market price minus the external comparable, over time.

This is the charter's Comparable Source column, kept as a series instead of a
single at-open reading. For every active sub-market that maps onto a
comparable (src/notion_sync.comparable_series_for -- FedWatch for Fed markets
today; FRED / polls when those land), append one row per run:

    market_p        latest tape YES price
    comparable_p    latest comparable reading for the mapped series
    divergence      market_p - comparable_p

Discipline boundary (charter Amendment A1): this module RECORDS divergence. It
does not rank it, threshold it, filter for "big" ones, or suggest a side.
Anything that does is phase 3 and does not belong here.

Usage
-----
    python -m src.divergence            # append one row per mapped active market
    python -m src.divergence --report   # print the latest row per market, write nothing
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Any

from src import db, notion_sync

log = logging.getLogger(__name__)

DIVERGENCE_SQL = """
CREATE TABLE IF NOT EXISTS divergence (
    div_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id         TEXT NOT NULL,
    timestamp         TEXT NOT NULL,            -- ISO UTC, when this row was computed
    market_p          REAL NOT NULL,
    market_ts         TEXT NOT NULL,
    comparable_source TEXT NOT NULL,
    comparable_series TEXT NOT NULL,
    comparable_p      REAL NOT NULL,
    comparable_ts     TEXT NOT NULL,
    divergence        REAL NOT NULL,            -- market_p - comparable_p
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_divergence_market_time
    ON divergence(market_id, timestamp);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DIVERGENCE_SQL)


def latest_price(conn: sqlite3.Connection, market_id: str) -> tuple[float | None, str | None]:
    row = conn.execute(
        "SELECT yes_price, timestamp FROM price_snapshots "
        "WHERE market_id = ? AND yes_price IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    return (row["yes_price"], row["timestamp"]) if row else (None, None)


def latest_comparable(conn: sqlite3.Connection, source: str, series_id: str) -> tuple[float | None, str | None]:
    row = conn.execute(
        "SELECT value, timestamp FROM comparables WHERE source = ? AND series_id = ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (source, series_id),
    ).fetchone()
    return (row["value"], row["timestamp"]) if row else (None, None)


def compute(conn: sqlite3.Connection, *, write: bool = True,
            now: str | None = None) -> list[dict[str, Any]]:
    """One row per active market with both a price and a comparable. Returns them."""
    now = now or db.utcnow_iso()
    out: list[dict[str, Any]] = []
    for m in db.get_active_markets(conn):
        row = dict(m)
        link = notion_sync.comparable_series_for(row)
        if not link:
            continue
        source, series_id = link
        market_p, market_ts = latest_price(conn, row["market_id"])
        comp_p, comp_ts = latest_comparable(conn, source, series_id)
        if market_p is None or comp_p is None:
            continue
        rec = {
            "market_id": row["market_id"],
            "question": row.get("question"),
            "timestamp": now,
            "market_p": market_p,
            "market_ts": market_ts,
            "comparable_source": source,
            "comparable_series": series_id,
            "comparable_p": comp_p,
            "comparable_ts": comp_ts,
            "divergence": round(market_p - comp_p, 6),
        }
        out.append(rec)
        if write:
            conn.execute(
                """
                INSERT INTO divergence (market_id, timestamp, market_p, market_ts,
                                        comparable_source, comparable_series,
                                        comparable_p, comparable_ts, divergence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rec["market_id"], now, market_p, market_ts, source, series_id,
                 comp_p, comp_ts, rec["divergence"]),
            )
    return out


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(prog="python -m src.divergence")
    ap.add_argument("--report", action="store_true", help="print only; write nothing")
    args = ap.parse_args()
    db.init_db()
    with db.get_connection() as conn:
        init(conn)
        rows = compute(conn, write=not args.report)
        if not rows:
            print("no market has both a tape price and a comparable reading yet")
        # Printed in tape order, deliberately NOT sorted by divergence.
        for r in rows:
            print(f"{r['question'][:55]:<55} mkt={r['market_p']:.3f} "
                  f"{r['comparable_series']}={r['comparable_p']:.3f} "
                  f"div={r['divergence']:+.3f}")
        log.info("wrote %d divergence row(s)", len(rows) if not args.report else 0)


if __name__ == "__main__":
    _main()
