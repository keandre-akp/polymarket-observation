"""Liveness and compliance checks. Exit non-zero on any failure so CI can alarm.

Three checks, each born from a failure that already happened:

* **staleness** -- the tape once went unwatched for months (the local clone
  was stale; nobody noticed). Fail if the newest price snapshot is older than
  ``--max-age-hours`` (default 6).

* **fedwatch-staleness** -- after disabling the CME scrape in Actions and
  moving it to Ke's Mac, FedWatch comparables silently stopped on days the
  Mac was off. The general tape-staleness alarm did not fire because the
  Polymarket poller kept the DB fresh. Fail if the newest ``cme_fedwatch``
  row in ``comparables`` is older than ``--fedwatch-max-age-hours``
  (default 36 -- one day plus a slack buffer for weekends and overnight).

* **forecast-due** (Amendment A7) -- three of the first four scoreable markets
  were lost because the read was not entered before the event. Fail if any
  tracked market resolves within ``--horizon-hours`` (default 48) and has no
  revision-0 forecast from ``--forecaster`` on record. The alarm keeps firing
  every run until the forecast lands or the market resolves.

Usage
-----
    python -m src.health                           # all checks
    python -m src.health --check staleness
    python -m src.health --check fedwatch-staleness
    python -m src.health --check forecast-due
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from src import db, forecasts, notion_sync

log = logging.getLogger(__name__)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_staleness(conn: sqlite3.Connection, *, max_age_hours: float = 6.0,
                    now: str | None = None) -> dict[str, Any]:
    now_dt = _parse(now or db.utcnow_iso())
    row = conn.execute("SELECT MAX(timestamp) AS ts FROM price_snapshots").fetchone()
    last = row["ts"] if row else None
    if not last:
        return {"ok": False, "last_snapshot": None, "age_hours": None,
                "message": "no price snapshots on the tape at all"}
    age = (now_dt - _parse(last)).total_seconds() / 3600.0
    ok = age <= max_age_hours
    return {
        "ok": ok, "last_snapshot": last, "age_hours": round(age, 2),
        "message": (f"tape fresh: last snapshot {last} ({age:.1f}h ago)" if ok
                    else f"TAPE STALE: last snapshot {last} is {age:.1f}h old (limit {max_age_hours}h)"),
    }


def check_fedwatch_staleness(conn: sqlite3.Connection, *,
                             max_age_hours: float = 36.0,
                             now: str | None = None) -> dict[str, Any]:
    """Fire if comparables holds no fresh cme_fedwatch row.

    36h is one business day plus a slack buffer -- a Mac-off overnight does
    not alarm, a Mac-off for two days does.
    """
    now_dt = _parse(now or db.utcnow_iso())
    row = conn.execute(
        "SELECT MAX(timestamp) AS ts FROM comparables WHERE source = 'cme_fedwatch'"
    ).fetchone()
    last = row["ts"] if row else None
    if not last:
        return {"ok": False, "last_fedwatch": None, "age_hours": None,
                "message": "no cme_fedwatch rows in comparables at all"}
    age = (now_dt - _parse(last)).total_seconds() / 3600.0
    ok = age <= max_age_hours
    return {
        "ok": ok, "last_fedwatch": last, "age_hours": round(age, 2),
        "message": (f"fedwatch fresh: last row {last} ({age:.1f}h ago)" if ok
                    else f"FEDWATCH STALE: last cme_fedwatch row {last} is "
                         f"{age:.1f}h old (limit {max_age_hours}h). "
                         f"Local scrape probably missed a day."),
    }


def check_forecast_due(conn: sqlite3.Connection, *, horizon_hours: float = 48.0,
                       forecaster: str = forecasts.DEFAULT_FORECASTER,
                       now: str | None = None) -> dict[str, Any]:
    """Tracked markets resolving within the horizon that have no first forecast."""
    now_iso = now or db.utcnow_iso()
    now_dt = _parse(now_iso)
    cutoff = now_dt + timedelta(hours=horizon_hours)
    rules = notion_sync.tracked_config()
    rollup = notion_sync.build_rollup(conn)
    due: list[dict[str, Any]] = []
    for row in rollup:
        if notion_sync.match_tracked(row, rules) is None:
            continue
        rd = row.get("resolution_date")
        if not rd:
            continue
        # Resolution is a calendar day; treat it as ending at 23:59:59 UTC that day.
        rd_dt = datetime.strptime(rd[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        if rd_dt <= now_dt:
            continue                       # already past -- nothing to chase
        if rd_dt > cutoff:
            continue                       # not yet inside the window
        if forecasts.first(conn, row["market_id"], forecaster) is not None:
            continue
        due.append({"market_id": row["market_id"], "question": row.get("question"),
                    "resolution_date": rd,
                    "hours_left": round((rd_dt - now_dt).total_seconds() / 3600.0, 1)})
    ok = not due
    msg = ("no forecasts due" if ok else
           "FORECAST DUE (" + forecaster + "): " +
           "; ".join(f"{d['question'][:50]} resolves {d['resolution_date']} "
                     f"({d['hours_left']}h left)" for d in due))
    return {"ok": ok, "due": due, "message": msg}


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(prog="python -m src.health")
    ap.add_argument("--check", choices=["all", "staleness", "fedwatch-staleness",
                                        "forecast-due"], default="all")
    ap.add_argument("--max-age-hours", type=float, default=6.0)
    ap.add_argument("--fedwatch-max-age-hours", type=float, default=36.0)
    ap.add_argument("--horizon-hours", type=float, default=48.0)
    ap.add_argument("--forecaster", default=forecasts.DEFAULT_FORECASTER)
    args = ap.parse_args()

    db.init_db()
    failures: list[str] = []
    with db.get_connection() as conn:
        forecasts.init(conn)
        if args.check in ("all", "staleness"):
            r = check_staleness(conn, max_age_hours=args.max_age_hours)
            print(r["message"])
            if not r["ok"]:
                failures.append(r["message"])
        if args.check in ("all", "fedwatch-staleness"):
            r = check_fedwatch_staleness(conn, max_age_hours=args.fedwatch_max_age_hours)
            print(r["message"])
            if not r["ok"]:
                failures.append(r["message"])
        if args.check in ("all", "forecast-due"):
            r = check_forecast_due(conn, horizon_hours=args.horizon_hours,
                                   forecaster=args.forecaster)
            print(r["message"])
            if not r["ok"]:
                failures.append(r["message"])
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
