"""Brier scoring: the forecaster against the market, on the same information.

Scores every (market, forecaster) pair that has a revision-0 forecast and a
settled outcome. Three Brier numbers per pair, all on the binary 0..1 scale
where 0 is perfect and 0.25 is the coin-flip baseline:

    brier_forecast             (p_forecast - y)^2
    brier_market_at_forecast   (p_market at the moment the forecast was logged - y)^2
    brier_market_close         (last live market price - y)^2   [reference only]

The head-to-head that matters is the first against the SECOND. The closing
price has every hour of information up to settlement; the forecast does not.
Scoring against close is a handicap match the forecaster loses by construction
on any liquid market. It is kept here because it is the right input for
hypothetical P&L, not because it is a fair calibration benchmark.

Outcomes come from the tape by default (works in CI without Notion): once the
resolution date has passed, a last price >= 0.99 is YES and <= 0.01 is NO.
Anything in between is still open and is skipped.

Usage
-----
    python -m src.brier            # score, write `scores`, print the report
    python -m src.brier --report   # report only, no writes
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import defaultdict
from typing import Any

from src import db, forecasts, resolve

log = logging.getLogger(__name__)

SCORES_SQL = """
CREATE TABLE IF NOT EXISTS scores (
    score_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id                TEXT    NOT NULL,
    forecaster               TEXT    NOT NULL,
    outcome                  INTEGER NOT NULL,     -- 1 = YES, 0 = NO
    forecast_p               REAL    NOT NULL,
    forecast_logged_at       TEXT    NOT NULL,
    market_p_at_forecast     REAL,
    market_p_close           REAL,
    brier_forecast           REAL    NOT NULL,
    brier_market_at_forecast REAL,
    brier_market_close       REAL,
    scored_at                TEXT    NOT NULL,
    UNIQUE (market_id, forecaster)
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCORES_SQL)


def brier(p: float | None, y: int) -> float | None:
    return None if p is None else round((p - y) ** 2, 6)


def outcome_from_tape(conn: sqlite3.Connection, market_id: str, resolution_date: str | None,
                      now: str | None = None) -> int | None:
    """1 / 0 once settled on the tape after resolution day, else None."""
    now = now or db.utcnow_iso()
    if not forecasts.resolution_passed(resolution_date, now):
        return None
    row = conn.execute(
        "SELECT yes_price FROM price_snapshots WHERE market_id = ? AND yes_price IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    if row is None:
        return None
    if row["yes_price"] >= resolve.SETTLED_HIGH:
        return 1
    if row["yes_price"] <= resolve.SETTLED_LOW:
        return 0
    return None


def score_all(conn: sqlite3.Connection, *, write: bool = True,
              now: str | None = None) -> list[dict[str, Any]]:
    """Score every revision-0 forecast whose market has settled. Returns the rows."""
    now = now or db.utcnow_iso()
    out: list[dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT f.market_id, f.forecaster, f.probability, f.logged_at, f.price_at_log,
               m.resolution_date, m.category, m.question
        FROM forecasts f JOIN markets m USING (market_id)
        WHERE f.revision = 0
        ORDER BY f.logged_at
        """
    ).fetchall()
    for r in rows:
        y = outcome_from_tape(conn, r["market_id"], r["resolution_date"], now)
        if y is None:
            continue
        close, _ = resolve.last_live_price(conn, r["market_id"])
        rec = {
            "market_id": r["market_id"],
            "forecaster": r["forecaster"],
            "category": r["category"],
            "question": r["question"],
            "outcome": y,
            "forecast_p": r["probability"],
            "forecast_logged_at": r["logged_at"],
            "market_p_at_forecast": r["price_at_log"],
            "market_p_close": close,
            "brier_forecast": brier(r["probability"], y),
            "brier_market_at_forecast": brier(r["price_at_log"], y),
            "brier_market_close": brier(close, y),
        }
        out.append(rec)
        if write:
            conn.execute(
                """
                INSERT INTO scores (market_id, forecaster, outcome, forecast_p, forecast_logged_at,
                                    market_p_at_forecast, market_p_close, brier_forecast,
                                    brier_market_at_forecast, brier_market_close, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id, forecaster) DO UPDATE SET
                    outcome = excluded.outcome,
                    forecast_p = excluded.forecast_p,
                    forecast_logged_at = excluded.forecast_logged_at,
                    market_p_at_forecast = excluded.market_p_at_forecast,
                    market_p_close = excluded.market_p_close,
                    brier_forecast = excluded.brier_forecast,
                    brier_market_at_forecast = excluded.brier_market_at_forecast,
                    brier_market_close = excluded.brier_market_close,
                    scored_at = excluded.scored_at
                """,
                (rec["market_id"], rec["forecaster"], y, rec["forecast_p"], rec["forecast_logged_at"],
                 rec["market_p_at_forecast"], rec["market_p_close"], rec["brier_forecast"],
                 rec["brier_market_at_forecast"], rec["brier_market_close"], now),
            )
    return out


def _mean(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def report(rows: list[dict[str, Any]]) -> str:
    """Plain-text summary: per forecaster overall, then per category."""
    if not rows:
        return "no scored forecasts yet (a forecast needs a settled market to score)"
    lines: list[str] = []
    by_f: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_f[r["forecaster"]].append(r)
    for forecaster, rs in sorted(by_f.items()):
        n = len(rs)
        bf = _mean([r["brier_forecast"] for r in rs])
        bm = _mean([r["brier_market_at_forecast"] for r in rs])
        bc = _mean([r["brier_market_close"] for r in rs])
        verdict = "-" if bm is None else ("beats market@forecast" if bf < bm else "behind market@forecast")
        lines.append(f"{forecaster}: n={n}  brier={bf}  market@forecast={bm}  market@close={bc}  {verdict}")
        by_c: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rs:
            by_c[r["category"] or "?"].append(r)
        for cat, crs in sorted(by_c.items()):
            lines.append(f"    {cat:<10} n={len(crs)}  brier={_mean([r['brier_forecast'] for r in crs])}  "
                         f"market@forecast={_mean([r['brier_market_at_forecast'] for r in crs])}")
        if n < 30:
            lines.append(f"    (n={n} < 30: not a verdict, per Gate 1)")
    return "\n".join(lines)


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(prog="python -m src.brier")
    ap.add_argument("--report", action="store_true", help="print only; do not write `scores`")
    args = ap.parse_args()
    db.init_db()
    with db.get_connection() as conn:
        forecasts.init(conn)
        init(conn)
        rows = score_all(conn, write=not args.report)
        print(report(rows))


if __name__ == "__main__":
    _main()
