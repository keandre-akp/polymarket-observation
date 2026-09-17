"""FRED daily pull -> the `comparables` table.

Writes DFF (effective fed funds rate), DFEDTARU and DFEDTARL (target range
upper/lower) with source = "fred", matching the source naming already used in
src/db.py.

Two things worth knowing:

* **Trailing window, not "today".** FRED publishes with a lag and revises.
  Asking only for today's date returns an empty `observations` list most
  mornings and leaves a permanent hole. We re-request the last N days every
  run instead.

* **The tape has no unique constraint**, so a trailing window would duplicate
  rows on every run. `save_comparable` in src/db.py is a plain INSERT by
  design -- price_snapshots genuinely wants every poll. A FRED print is not
  like that: DFF for 2026-08-04 is one fact with one value. So this module
  checks (source, series_id, timestamp) before inserting, and updates in place
  when FRED revises a number it already published.

Run from repo root:
    python -m src.fred                  # trailing window
    python -m src.fred --backfill       # ~2 years
    python -m src.fred --start 2026-01-01
    python -m src.fred --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from typing import Any

import requests

from src import config, db

FRED_API_BASE = "https://api.stlouisfed.org/fred"
SOURCE = "fred"

REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = 1.5

log = logging.getLogger("fred")


class FredError(RuntimeError):
    pass


# ---------- HTTP ----------

def _get_observations(
    series_id: str, start: str, end: str, api_key: str
) -> list[dict[str, str]]:
    """Fetch one series between two dates. Retries on 429/5xx."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
        "observation_end": end,
        "sort_order": "asc",
    }
    url = f"{FRED_API_BASE}/series/observations"

    last_exc: Exception | None = None
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 400:
                # FRED puts a human-readable reason in the body for a bad key.
                raise FredError(f"FRED rejected {series_id}: {resp.text[:300]}")
            if resp.status_code == 429 or resp.status_code >= 500:
                raise FredError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json().get("observations", [])
        except FredError as exc:
            if "rejected" in str(exc):
                raise
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 -- network/parse
            last_exc = exc
        log.warning("GET %s attempt %d/%d failed: %s",
                    series_id, attempt, RETRY_ATTEMPTS, last_exc)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(delay)
            delay *= 2

    raise FredError(f"could not fetch {series_id}: {last_exc}")


# ---------- Parsing ----------

def to_points(series_id: str, observations: list[dict[str, str]]) -> list[dict[str, Any]]:
    """FRED's string payload -> comparable points, dropping '.' gaps.

    FRED uses "." for holidays and not-yet-published days. Writing those as
    0.0 would quietly corrupt every average downstream.
    """
    points: list[dict[str, Any]] = []
    for obs in observations:
        raw = (obs.get("value") or "").strip()
        if raw in ("", "."):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        points.append(
            {
                "series_id": series_id,
                # Timestamps on this tape are ISO-8601 Z. A FRED observation is
                # a daily figure, so it is stamped at midnight UTC of its date.
                "timestamp": f"{obs['date']}T00:00:00Z",
                "value": value,
                "notes": "fred daily pull",
            }
        )
    return points


# ---------- Persistence ----------

def save_point(conn, point: dict[str, Any]) -> str:
    """Insert, or update in place if FRED revised a value we already hold.

    Returns "inserted", "updated" or "unchanged".
    """
    existing = conn.execute(
        "SELECT comp_id, value FROM comparables "
        "WHERE source = ? AND series_id = ? AND timestamp = ?",
        (SOURCE, point["series_id"], point["timestamp"]),
    ).fetchone()

    if existing is None:
        db.save_comparable(
            conn,
            source=SOURCE,
            series_id=point["series_id"],
            value=point["value"],
            timestamp=point["timestamp"],
            notes=point.get("notes"),
        )
        return "inserted"

    if abs(existing["value"] - point["value"]) < 1e-9:
        return "unchanged"

    conn.execute(
        "UPDATE comparables SET value = ?, notes = ? WHERE comp_id = ?",
        (point["value"], "fred revision", existing["comp_id"]),
    )
    return "updated"


def derive_target_midpoint(conn) -> int:
    """Write DFEDTARMID = (upper + lower) / 2 for every day we hold both.

    Not a FRED series. It is the number you compare a "where will the rate be"
    market against, so it gets computed once here rather than in every
    downstream query.
    """
    rows = conn.execute(
        """
        SELECT u.timestamp AS ts, (u.value + l.value) / 2.0 AS mid
        FROM comparables u
        JOIN comparables l
          ON l.source = ? AND l.series_id = 'DFEDTARL' AND l.timestamp = u.timestamp
        WHERE u.source = ? AND u.series_id = 'DFEDTARU'
        """,
        (SOURCE, SOURCE),
    ).fetchall()

    written = 0
    for row in rows:
        result = save_point(
            conn,
            {
                "series_id": "DFEDTARMID",
                "timestamp": row["ts"],
                "value": row["mid"],
                "notes": "derived from DFEDTARU + DFEDTARL",
            },
        )
        if result != "unchanged":
            written += 1
    return written


# ---------- Entry point ----------

def run(
    start: str | None = None,
    end: str | None = None,
    *,
    dry_run: bool = False,
    backfill: bool = False,
) -> dict[str, int]:
    cfg = config.section("fred")
    series_ids = cfg.get("series") or ["DFF", "DFEDTARU", "DFEDTARL"]
    api_key = config.require_env("FRED_API_KEY")

    today = date.today()
    end = end or today.isoformat()
    if start is None:
        days = cfg.get("backfill_days", 730) if backfill else cfg.get("lookback_days", 14)
        start = (today - timedelta(days=days)).isoformat()

    log.info("pulling %s over [%s .. %s]", ", ".join(series_ids), start, end)

    all_points: list[dict[str, Any]] = []
    for series_id in series_ids:
        observations = _get_observations(series_id, start, end, api_key)
        points = to_points(series_id, observations)
        latest = points[-1] if points else None
        log.info(
            "  %-9s %3d usable obs%s",
            series_id,
            len(points),
            f"   latest {latest['timestamp'][:10]} = {latest['value']:.2f}%" if latest else "",
        )
        all_points.extend(points)

    if dry_run:
        log.info("[dry-run] would write %d points; nothing saved", len(all_points))
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    db.init_db()
    with db.get_connection() as conn:
        for point in all_points:
            stats[save_point(conn, point)] += 1
        derived = derive_target_midpoint(conn)

    log.info(
        "inserted %d, updated %d, unchanged %d (+%d DFEDTARMID)",
        stats["inserted"], stats["updated"], stats["unchanged"], derived,
    )
    return stats


def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Pull FRED rate series into comparables.")
    p.add_argument("--start", help="observation_start (YYYY-MM-DD)")
    p.add_argument("--end", help="observation_end (YYYY-MM-DD)")
    p.add_argument("--backfill", action="store_true", help="pull ~2 years")
    p.add_argument("--dry-run", action="store_true", help="fetch but do not write")
    args = p.parse_args()
    run(args.start, args.end, dry_run=args.dry_run, backfill=args.backfill)


if __name__ == "__main__":
    _main()
