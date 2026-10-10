#!/usr/bin/env python3
"""Notion Observation Log watcher -- local-only glue, not part of CI.

Reads the Observation Log pages the pipeline already tracks (via
notion_sync_state, populated by the daily sync) and reports two things a
human would otherwise have to notice by eye:

1. Outcome change: a tracked page's "Actual Outcome" select differs from
   what we saw last time we checked. Catches a hand-edit in Notion (someone
   resolved a market) between syncs.
2. Forecast due: reuses src.health.check_forecast_due -- any tracked market
   resolving within --horizon-hours (default 48) with no revision-0 forecast
   on record.

No LLM, no edits -- read-only against both Notion and the local DB. State
(last-seen outcome per page) lives in ops/.notion_watch_state.json, which is
gitignored; losing it just means the next run treats every page's current
outcome as a fresh baseline instead of a diff.

Usage:
    python -m ops.notion_watch               # print only
    python -m ops.notion_watch --horizon-hours 48
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import db, health, notion_sync  # noqa: E402

STATE_PATH = REPO_ROOT / "ops" / ".notion_watch_state.json"


def _load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def _outcome_of(page_id: str) -> str | None:
    page = notion_sync._call("GET", f"/pages/{page_id}")
    prop = (page.get("properties") or {}).get("Actual Outcome") or {}
    sel = prop.get("select")
    return sel.get("name") if sel else None


def check_outcome_changes() -> list[dict[str, str]]:
    """Diff each tracked page's current Actual Outcome against last-seen state."""
    db.init_db()
    changes: list[dict[str, str]] = []
    state = _load_state()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT market_id, notion_page_id FROM notion_sync_state"
        ).fetchall()
    # market_id -> question, for a readable message
    with db.get_connection() as conn:
        questions = {r["market_id"]: r["question"] for r in
                     conn.execute("SELECT market_id, question FROM markets")}

    new_state = dict(state)
    for row in rows:
        market_id, page_id = row["market_id"], row["notion_page_id"]
        try:
            current = _outcome_of(page_id)
        except notion_sync.NotionError as exc:
            print(f"  warn: could not read page {page_id} ({market_id[:12]}...): {exc}",
                  file=sys.stderr)
            continue
        previous = state.get(page_id)
        new_state[page_id] = current or ""
        if previous is not None and previous != (current or ""):
            changes.append({
                "market_id": market_id,
                "question": questions.get(market_id, "?"),
                "from": previous or "(none)",
                "to": current or "(none)",
            })
    _save_state(new_state)
    return changes


def _main() -> None:
    ap = argparse.ArgumentParser(prog="python -m ops.notion_watch")
    ap.add_argument("--horizon-hours", type=float, default=48.0)
    args = ap.parse_args()

    out_lines: list[str] = []

    changes = check_outcome_changes()
    for c in changes:
        out_lines.append(
            f"OUTCOME CHANGED: {c['question'][:70]} -- {c['from']} -> {c['to']}"
        )

    db.init_db()
    with db.get_connection() as conn:
        due = health.check_forecast_due(conn, horizon_hours=args.horizon_hours)
    if not due["ok"]:
        for d in due["due"]:
            out_lines.append(
                f"FORECAST DUE: {d['question'][:70]} resolves {d['resolution_date']} "
                f"({d['hours_left']}h left)"
            )

    if out_lines:
        print("Polymarket Observation Log -- changes since last check:")
        for line in out_lines:
            print(f"  - {line}")
    else:
        print("Polymarket Observation Log: no outcome changes, no forecasts due.")


if __name__ == "__main__":
    _main()
