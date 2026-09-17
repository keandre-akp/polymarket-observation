"""Forecast capture: the timestamped, append-only record of every probability
a forecaster committed to BEFORE a market resolved.

Why this exists
---------------
The Notion column ``My Independent Read`` holds a number but not *when* it was
entered, and Notion's last-edited time moves on any edit. Without a trustworthy
timestamp there is no way to prove a read was price-blind and pre-resolution,
and the whole calibration test rests on that.

So the pipeline keeps its own record. Each run reads the Notion row for every
tracked market; the first time a value appears it is written here with the
time the pipeline saw it and the tape price at that moment. That timestamp is
honest to within one job cadence -- it can be late, never early.

Rules enforced mechanically (not by memory):

* **No backdating.** ``logged_at`` is always "now". There is no flag to set it.
* **No hindsight.** A forecast is refused once the market's resolution date has
  passed or the tape shows it settled.
* **First capture wins.** Later edits are stored as revisions for the audit
  trail; scoring (src/brier.py) reads revision 0 only.

Usage
-----
    python -m src.forecasts capture            # pull from Notion (needs NOTION_API_KEY)
    python -m src.forecasts capture --dry-run
    python -m src.forecasts log --market <id> --forecaster claude --p 0.63 [--notes ...]
    python -m src.forecasts list
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src import db, notion_sync

log = logging.getLogger(__name__)

DEFAULT_FORECASTER = "keandre"

FORECASTS_SQL = """
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT    NOT NULL,
    forecaster      TEXT    NOT NULL,            -- keandre / claude / ...
    probability     REAL    NOT NULL,            -- P(YES), 0..1
    logged_at       TEXT    NOT NULL,            -- ISO UTC, when the pipeline first saw it
    price_at_log    REAL,                        -- tape YES price nearest logged_at
    price_at_log_ts TEXT,
    source          TEXT    NOT NULL,            -- notion / cli
    revision        INTEGER NOT NULL DEFAULT 0,  -- 0 = first capture; scoring uses 0 only
    notes           TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_lookup
    ON forecasts(market_id, forecaster, revision);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(FORECASTS_SQL)


# --------------------------------------------------------------------------
# tape helpers
# --------------------------------------------------------------------------

def price_at(conn: sqlite3.Connection, market_id: str, ts: str) -> tuple[float | None, str | None]:
    """Tape YES price nearest ``ts``: latest on-or-before, else earliest after."""
    row = conn.execute(
        "SELECT yes_price, timestamp FROM price_snapshots "
        "WHERE market_id = ? AND yes_price IS NOT NULL AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (market_id, ts),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT yes_price, timestamp FROM price_snapshots "
            "WHERE market_id = ? AND yes_price IS NOT NULL AND timestamp > ? "
            "ORDER BY timestamp ASC LIMIT 1",
            (market_id, ts),
        ).fetchone()
    return (row["yes_price"], row["timestamp"]) if row else (None, None)


def is_settled_on_tape(conn: sqlite3.Connection, market_id: str, as_of: str,
                       high: float = 0.99, low: float = 0.01) -> bool:
    """True if the newest tape tick on or before ``as_of`` sits at an extreme."""
    row = conn.execute(
        "SELECT yes_price FROM price_snapshots WHERE market_id = ? AND yes_price IS NOT NULL "
        "AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (market_id, as_of),
    ).fetchone()
    return bool(row) and (row["yes_price"] >= high or row["yes_price"] <= low)


def resolution_passed(resolution_date: str | None, now: str) -> bool:
    """True once the calendar day of resolution has ended (UTC)."""
    if not resolution_date:
        return False
    return now[:10] > resolution_date[:10]


# --------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------

class ForecastRefused(ValueError):
    """The forecast would violate the no-hindsight rule."""


def latest(conn: sqlite3.Connection, market_id: str, forecaster: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM forecasts WHERE market_id = ? AND forecaster = ? "
        "ORDER BY revision DESC LIMIT 1",
        (market_id, forecaster),
    ).fetchone()


def first(conn: sqlite3.Connection, market_id: str, forecaster: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM forecasts WHERE market_id = ? AND forecaster = ? AND revision = 0",
        (market_id, forecaster),
    ).fetchone()


def record(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    forecaster: str,
    probability: float,
    source: str,
    notes: str | None = None,
    now: str | None = None,
) -> str:
    """Append a forecast. Returns 'inserted', 'revised', or 'unchanged'.

    Raises ForecastRefused if the market has already resolved. ``now`` is
    injectable for tests only; the CLI never exposes it.
    """
    if not (0.0 <= probability <= 1.0):
        raise ValueError(f"probability must be in [0, 1], got {probability}")
    now = now or db.utcnow_iso()

    market = conn.execute(
        "SELECT market_id, resolution_date FROM markets WHERE market_id = ?", (market_id,)
    ).fetchone()
    if market is None:
        raise ValueError(f"unknown market_id {market_id!r} -- run the poller's discover first")
    if resolution_passed(market["resolution_date"], now):
        raise ForecastRefused(
            f"{market_id[:12]}: resolution date {market['resolution_date']} has passed -- "
            "forecasts are not accepted after the fact"
        )
    if is_settled_on_tape(conn, market_id, now):
        raise ForecastRefused(f"{market_id[:12]}: tape shows this market settled")

    prev = latest(conn, market_id, forecaster)
    if prev is not None and abs(prev["probability"] - probability) < 1e-9:
        return "unchanged"
    revision = 0 if prev is None else prev["revision"] + 1

    price, price_ts = price_at(conn, market_id, now)
    conn.execute(
        """
        INSERT INTO forecasts (market_id, forecaster, probability, logged_at,
                               price_at_log, price_at_log_ts, source, revision, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (market_id, forecaster, probability, now, price, price_ts, source, revision, notes),
    )
    return "inserted" if revision == 0 else "revised"


def _page_id_for(conn: sqlite3.Connection, rule: dict[str, Any], market_id: str) -> str | None:
    if rule.get("notion_page_id"):
        return rule["notion_page_id"]
    row = conn.execute(
        "SELECT notion_page_id FROM notion_sync_state WHERE market_id = ?", (market_id,)
    ).fetchone()
    return row["notion_page_id"] if row else None


def capture_from_notion(conn: sqlite3.Connection, *, dry_run: bool = False,
                        forecaster: str = DEFAULT_FORECASTER) -> dict[str, int]:
    """Read ``My Independent Read`` for every tracked market and record new values."""
    from src import resolve  # local import: resolve pulls in requests/Gamma bits

    stats = {"inserted": 0, "revised": 0, "unchanged": 0, "empty": 0, "refused": 0, "failed": 0}
    conn.executescript(notion_sync.SYNC_STATE_SQL)
    rules = notion_sync.tracked_config()
    rollup = notion_sync.build_rollup(conn)

    for row in rollup:
        rule = notion_sync.match_tracked(row, rules)
        if rule is None:
            continue
        market_id = row["market_id"]
        page_id = _page_id_for(conn, rule, market_id)
        if not page_id:
            continue
        try:
            current = resolve.read_notion_row(page_id)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            log.error("could not read Notion row for %s: %s", row.get("question"), exc)
            continue
        read = current.get("independent_read")
        if read is None:
            stats["empty"] += 1
            continue
        # Percent-formatted Notion columns store fractions; tolerate a whole-number slip.
        p = float(read)
        if p > 1.0:
            p = p / 100.0

        label = (row.get("question") or market_id)[:60]
        if dry_run:
            prev = latest(conn, market_id, forecaster)
            state = "new" if prev is None else ("same" if abs(prev["probability"] - p) < 1e-9 else "changed")
            print(f"[dry-run] {label}: read={p:.3f} ({state})")
            continue
        try:
            result = record(conn, market_id=market_id, forecaster=forecaster,
                            probability=p, source="notion")
            stats[result] += 1
            if result != "unchanged":
                log.info("%s: %s %.3f", label, result, p)
        except ForecastRefused as exc:
            stats["refused"] += 1
            log.warning("refused: %s", exc)
    return stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(prog="python -m src.forecasts")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="record new Independent Reads from Notion")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--forecaster", default=DEFAULT_FORECASTER)

    lg = sub.add_parser("log", help="record a forecast from the command line (benchmarks)")
    lg.add_argument("--market", required=True, help="market_id (condition id)")
    lg.add_argument("--forecaster", required=True)
    lg.add_argument("--p", type=float, required=True, help="P(YES) in [0, 1]")
    lg.add_argument("--notes")

    sub.add_parser("list", help="print every forecast on record")

    args = ap.parse_args()
    db.init_db()
    with db.get_connection() as conn:
        init(conn)
        if args.cmd == "capture":
            stats = capture_from_notion(conn, dry_run=args.dry_run, forecaster=args.forecaster)
            print(stats)
            if stats["failed"]:
                raise SystemExit(1)
        elif args.cmd == "log":
            try:
                print(record(conn, market_id=args.market, forecaster=args.forecaster,
                             probability=args.p, source="cli", notes=args.notes))
            except (ForecastRefused, ValueError) as exc:
                raise SystemExit(f"refused: {exc}")
        elif args.cmd == "list":
            rows = conn.execute(
                "SELECT f.forecaster, f.revision, f.probability, f.logged_at, f.price_at_log, "
                "m.question FROM forecasts f JOIN markets m USING (market_id) "
                "ORDER BY f.logged_at"
            ).fetchall()
            for r in rows:
                print(f"{r['logged_at']}  {r['forecaster']:<10} r{r['revision']}  "
                      f"p={r['probability']:.3f}  mkt@log={r['price_at_log']}  {r['question'][:60]}")
            if not rows:
                print("no forecasts on record")


if __name__ == "__main__":
    _main()
