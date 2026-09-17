"""SQLite rollup -> the Notion Observation Log.

Reads `markets`, `price_snapshots` and `comparables`, and upserts one Notion
page per tracked sub-market.

Two rules this module will not break
-----------------------------------
1. **It only writes system-owned properties.** The allowlist and denylist both
   live in config/comparables.yml. Your columns -- My Independent Read, Edge
   Call, Hypothetical P&L, Notes -- are never in a request body, so a sync can
   never overwrite your own analysis. There is a test that fails if that stops
   being true.

2. **It updates, never duplicates.** Each market's page id is cached in
   `notion_sync_state`; on a cold cache it looks the page up by title before
   creating anything. A payload hash per market means a run where nothing
   changed makes zero write calls.

On matching a sub-market to a comparable
----------------------------------------
No manual link table -- the tape already carries enough. Two market shapes get
a comparable:

    "No change" + resolution_date 2026-09-16 (an FOMC date)
        -> cme_fedwatch / FOMC:2026-09-16:HOLD

    "3.75%" + resolution_date 2026-12-31 (a rate level)
        -> cme_fedwatch / FOMC:2026-12-09:350-375
           (last meeting on or before resolution; the bucket whose upper
            bound is 375 bps)

Everything else gets none, deliberately. "Will 3 Fed rate cuts happen in 2026?"
contains the word "cut" but resolves on 2026-12-31, which is not a meeting, and
FedWatch publishes no cuts-per-year series. A confident wrong comparable is
worse than an empty one.

FOMC dates come from config/events.yml; the outcome word lists are in
config/comparables.yml.

Note the Observation Log's "Market" title is the sub-market question, not the
event name: sub-markets of one event share a polymarket_url, so the question is
the only thing that distinguishes them.

Run from repo root:
    python -m src.notion_sync --verify-schema
    python -m src.notion_sync --dry-run
    python -m src.notion_sync
    python -m src.notion_sync --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from src import config, db

NOTION_API_BASE = "https://api.notion.com/v1"
# Notion's documented ceiling is ~3 requests/second per integration.
MIN_INTERVAL_S = 0.34

log = logging.getLogger("notion_sync")

VALID_CATEGORIES = {"Fed", "Economic", "Election"}
VALID_SOURCES = {"FedWatch", "FRED", "Odds API", "Polls", "None"}
VALID_OUTCOMES = {"YES", "NO", "Unresolved"}

# comparables.source -> the Notion "Comparable Source" select option.
SOURCE_LABELS = {"cme_fedwatch": "FedWatch", "fred": "FRED", "odds_api": "Odds API"}

SYNC_STATE_SQL = """
CREATE TABLE IF NOT EXISTS notion_sync_state (
    market_id      TEXT PRIMARY KEY,
    notion_page_id TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    payload_hash   TEXT
);
"""

_last_call = 0.0


class NotionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.require_env('NOTION_API_KEY')}",
        "Notion-Version": config.section("notion").get("api_version", "2022-06-28"),
        "Content-Type": "application/json",
    }


def _call(method: str, path: str, payload: dict[str, Any] | None = None,
          *, max_retries: int = 5) -> dict[str, Any]:
    global _last_call
    url = f"{NOTION_API_BASE}{path}"
    for attempt in range(max_retries):
        elapsed = time.monotonic() - _last_call
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)
        _last_call = time.monotonic()

        resp = requests.request(method, url, headers=_headers(), json=payload, timeout=30)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("rate limited; sleeping %.1fs", wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            raise NotionError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()
    raise NotionError(f"{method} {path} failed after {max_retries} attempts")


def _database_id() -> str:
    return config.section("notion")["database_id"]


# --------------------------------------------------------------------------
# Rollup
# --------------------------------------------------------------------------

ROLLUP_SQL = """
WITH snaps AS (
    SELECT market_id, timestamp, yes_price
    FROM price_snapshots
    WHERE yes_price IS NOT NULL
),
span AS (
    SELECT market_id,
           MIN(JULIANDAY(timestamp)) AS j_first,
           MAX(JULIANDAY(timestamp)) AS j_last,
           MIN(timestamp) AS first_ts,
           MAX(timestamp) AS last_ts,
           COUNT(*)       AS snapshot_count
    FROM snaps
    GROUP BY market_id
),
ranked AS (
    SELECT s.market_id, s.timestamp, s.yes_price,
           ROW_NUMBER() OVER (PARTITION BY s.market_id ORDER BY s.timestamp ASC)  AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY s.market_id ORDER BY s.timestamp DESC) AS rn_last,
           ROW_NUMBER() OVER (
               PARTITION BY s.market_id
               ORDER BY ABS(JULIANDAY(s.timestamp) - (p.j_first + p.j_last) / 2.0) ASC,
                        s.timestamp ASC
           ) AS rn_mid
    FROM snaps s
    JOIN span p ON p.market_id = s.market_id
)
SELECT
    m.market_id, m.event_slug, m.event_name, m.category, m.question,
    m.outcome_name, m.polymarket_url, m.resolution_date, m.active,
    DATE(p.first_ts)                 AS open_date,
    DATE(p.last_ts)                  AS last_date,
    COALESCE(p.snapshot_count, 0)    AS snapshot_count,
    f.yes_price                      AS opening_probability,
    d.yes_price                      AS mid_probability,
    t.yes_price                      AS closing_probability
FROM markets m
LEFT JOIN span   p ON p.market_id = m.market_id
LEFT JOIN ranked f ON f.market_id = m.market_id AND f.rn_first = 1
LEFT JOIN ranked d ON d.market_id = m.market_id AND d.rn_mid   = 1
LEFT JOIN ranked t ON t.market_id = m.market_id AND t.rn_last  = 1
ORDER BY m.event_slug, m.question
"""


def direction_for(outcome_name: str | None, question: str | None) -> str | None:
    """Map a sub-market's outcome label onto HOLD / CUT / HIKE.

    Matching is on whole words, not substrings. Naive `in` matching maps
    "25 bps increase" to CUT, because "ease" is a substring of "incr-ease-".
    """
    haystack = f"{outcome_name or ''} {question or ''}".lower()
    for rule in config.section("notion").get("outcome_direction_map", []):
        for needle in rule.get("match", []):
            pattern = r"\b" + r"\s+".join(re.escape(w) for w in needle.lower().split()) + r"\b"
            if re.search(pattern, haystack):
                return rule["direction"]
    return None


LEVEL_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)\s*%?\s*$")


def fomc_dates() -> list[str]:
    """FOMC meeting dates from config/events.yml, ascending.

    Reusing the calendar you already maintain, rather than keeping a second
    copy of it in here.
    """
    path = config.REPO_ROOT / "config" / "events.yml"
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []
    return sorted(
        str(e["date"]) for e in (data.get("events") or [])
        if e.get("type") == "FOMC" and e.get("date")
    )


def last_fomc_on_or_before(target: str) -> str | None:
    earlier = [d for d in fomc_dates() if d <= target]
    return earlier[-1] if earlier else None


def level_bucket_for(outcome_name: str | None) -> str | None:
    """'3.75%' -> '350-375'.

    The "rate at end of 2026" markets are priced on the *upper bound* of the
    target range, and a FedWatch bucket is named by its bounds in basis points.
    So an outcome of 3.75% is the bucket whose upper bound is 375.

    Open-ended buckets (">= 4.5%", "<=1.0%") map to no single FedWatch bucket
    and return None rather than being forced into the nearest one.
    """
    m = LEVEL_RE.match((outcome_name or "").strip())
    if not m:
        return None
    hi = int(round(float(m.group(1)) * 100))
    lo = hi - 25
    return f"{lo}-{hi}" if lo >= 0 else None


def comparable_series_for(row: dict[str, Any]) -> tuple[str, str] | None:
    """(source, series_id) for a sub-market, or None if we cannot tell.

    Two shapes get a comparable, and everything else deliberately gets none:

    * **Meeting-outcome markets** -- resolution_date is an actual FOMC date, so
      the outcome maps onto that meeting's HOLD / CUT / HIKE probability.
    * **Rate-level markets** -- the outcome is a rate level, so it maps onto the
      FedWatch bucket for the last FOMC meeting on or before resolution.

    The FOMC-date check is what stops "Will 3 Fed rate cuts happen in 2026?"
    being labelled with a FedWatch comparable. It contains the word "cut", but
    it resolves on 2026-12-31, which is not a meeting -- and FedWatch has no
    cuts-per-year series to compare it against. Better no comparable than a
    confident wrong one.
    """
    if (row.get("category") or "") != "Fed":
        return None
    resolution_date = row.get("resolution_date")
    if not resolution_date:
        return None

    if resolution_date in fomc_dates():
        direction = direction_for(row.get("outcome_name"), row.get("question"))
        if direction:
            return ("cme_fedwatch", f"FOMC:{resolution_date}:{direction}")
        return None

    bucket = level_bucket_for(row.get("outcome_name"))
    if bucket:
        meeting = last_fomc_on_or_before(resolution_date)
        if meeting:
            return ("cme_fedwatch", f"FOMC:{meeting}:{bucket}")
    return None


def comparable_at_open(
    conn: sqlite3.Connection, source: str, series_id: str, open_date: str | None
) -> sqlite3.Row | None:
    """The reading closest to the market's open date.

    Prefer the most recent on-or-before; fall back to the earliest after.
    FedWatch scraping starts the day this ships, and markets predate that, so
    a strict on-or-before rule would leave every existing market empty forever.
    """
    if not open_date:
        return conn.execute(
            "SELECT value, timestamp FROM comparables "
            "WHERE source = ? AND series_id = ? ORDER BY timestamp ASC LIMIT 1",
            (source, series_id),
        ).fetchone()
    return conn.execute(
        """
        SELECT value, timestamp FROM comparables
        WHERE source = ? AND series_id = ?
        ORDER BY
            CASE WHEN DATE(timestamp) <= ? THEN 0 ELSE 1 END ASC,
            ABS(JULIANDAY(timestamp) - JULIANDAY(?)) ASC,
            timestamp ASC
        LIMIT 1
        """,
        (source, series_id, open_date, open_date),
    ).fetchone()


def build_rollup(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(ROLLUP_SQL)]
    for row in rows:
        link = comparable_series_for(row)
        row["comparable_source"] = None
        row["comparable_series"] = None
        row["comparable_value"] = None
        row["comparable_date"] = None
        if not link:
            continue
        source, series_id = link
        hit = comparable_at_open(conn, source, series_id, row.get("open_date"))
        row["comparable_source"] = source
        row["comparable_series"] = series_id
        if hit:
            row["comparable_value"] = hit["value"]
            row["comparable_date"] = hit["timestamp"]
    return rows


# --------------------------------------------------------------------------
# Notion property builders
# --------------------------------------------------------------------------

def _title(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": (text or "")[:2000]}}]}


def _select(name: str | None) -> dict[str, Any]:
    return {"select": {"name": name} if name else None}


def _number(value: float | None) -> dict[str, Any]:
    return {"number": None if value is None else round(float(value), 6)}


def _date(iso: str | None) -> dict[str, Any]:
    return {"date": {"start": iso[:10]} if iso else None}


def _url(value: str | None) -> dict[str, Any]:
    return {"url": value or None}


def build_properties(row: dict[str, Any]) -> dict[str, Any]:
    """One rollup row -> Notion properties. System columns only."""
    notion_cfg = config.section("notion")
    system = set(notion_cfg.get("system_properties", []))
    user = set(notion_cfg.get("user_properties", []))

    category = row.get("category")
    category = category if category in VALID_CATEGORIES else None

    source_label = SOURCE_LABELS.get(row.get("comparable_source") or "", "None")
    if source_label not in VALID_SOURCES:
        source_label = "None"

    # A comparable is only a *probability* if it is measured as one. FedWatch
    # gives probabilities; a FRED rate level is a percent, and writing 3.88
    # into a percent-formatted probability field would render 388%.
    comparable_probability = (
        row.get("comparable_value") if row.get("comparable_source") == "cme_fedwatch"
        else None
    )

    props: dict[str, Any] = {
        "Market": _title(row.get("question") or row.get("event_name") or "(untitled)"),
        "Polymarket URL": _url(row.get("polymarket_url")),
        "Open Date": _date(row.get("open_date")),
        "Resolution Date": _date(row.get("resolution_date")),
        "Opening Probability": _number(row.get("opening_probability")),
        "Mid-Period Probability": _number(row.get("mid_probability")),
        "Closing Probability": _number(row.get("closing_probability")),
        "Comparable Probability": _number(comparable_probability),
        "Comparable Source": _select(source_label),
        "Actual Outcome": _select("Unresolved"),
    }
    if category:
        props["Category"] = _select(category)

    leaked = set(props) & user
    if leaked:  # belt and braces -- must never happen
        raise NotionError(f"refusing to write user-owned properties: {leaked}")
    unknown = set(props) - system
    if unknown:
        raise NotionError(f"property not in the system allowlist: {unknown}")
    return props


# Properties the pipeline may fill ONCE, never overwrite. The seven observation
# rows were created by hand on Sep 9 and the tape only started seeing those
# markets on Sep 17: a naive sync would replace the hand-entered Sep 9 opening
# probabilities with Sep 17 prices, and flip hand-logged YES/NO outcomes back
# to "Unresolved". Discovered Sep 17 before the first live sync; never shipped.
PROTECTED_ONCE_SET = frozenset({
    "Opening Probability", "Open Date", "Closing Probability", "Actual Outcome",
})


def _has_value(prop: dict[str, Any] | None) -> bool:
    if not prop:
        return False
    t = prop.get("type")
    if t == "number":
        return prop.get("number") is not None
    if t == "select":
        sel = prop.get("select")
        return bool(sel) and sel.get("name") not in (None, "", "Unresolved")
    if t == "date":
        return prop.get("date") is not None
    if t in ("title", "rich_text"):
        return bool(prop.get(t))
    if t == "url":
        return bool(prop.get("url"))
    return prop.get(t) is not None


def existing_values(page_id: str) -> dict[str, bool]:
    """{property name: already has a value} for one Notion page."""
    page = _call("GET", f"/pages/{page_id}")
    return {name: _has_value(prop) for name, prop in (page.get("properties") or {}).items()}


def protect_existing(props: dict[str, Any], existing: dict[str, bool]) -> dict[str, Any]:
    """Drop every once-only property that Notion already holds a value for."""
    return {k: v for k, v in props.items()
            if not (k in PROTECTED_ONCE_SET and existing.get(k))}


def payload_hash(props: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(props, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


# --------------------------------------------------------------------------
# Which sub-markets belong in the log, and which row each one is
# --------------------------------------------------------------------------

MARKETS_CONFIG = config.REPO_ROOT / "config" / "markets.yml"


def _norm(text: str | None) -> str:
    """Loose comparison key: lowercase, strip punctuation and spaces.

    Polymarket writes bracket labels inconsistently ("0-50k", "0 - 50K",
    "add 0-50k"), so exact matching on outcome labels is too brittle to be the
    only rule.
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def tracked_config(path: Path = MARKETS_CONFIG) -> list[dict[str, Any]]:
    """Flatten config/markets.yml into one entry per tracked sub-market."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    out: list[dict[str, Any]] = []
    for event in data.get("events", []) or []:
        if not event.get("active", True):
            continue
        for rule in event.get("track", []) or []:
            out.append({
                "event_slug": event["slug"],
                "event_name": event.get("name"),
                "outcome": rule.get("outcome"),
                "notion_page_id": rule.get("notion_page_id"),
            })
    return out


def match_tracked(row: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The track rule covering this sub-market, or None if it is untracked.

    Tries exact-ish match on the normalised outcome label first, then a
    substring fallback, then the question text. Untracked sub-markets are still
    polled into the tape -- they just do not get a Notion row.
    """
    candidates = [r for r in rules if r["event_slug"] == row.get("event_slug")]
    if not candidates:
        return None
    outcome = _norm(row.get("outcome_name"))
    question = _norm(row.get("question"))
    for rule in candidates:
        if _norm(rule["outcome"]) == outcome:
            return rule
    for rule in candidates:
        needle = _norm(rule["outcome"])
        if needle and (needle in outcome or needle in question):
            return rule
    return None


def link_report(conn: sqlite3.Connection) -> int:
    """Show what each track rule resolved to, without writing anything.

    Worth running after any change to config/markets.yml. A rule that matches
    nothing means the sync would silently skip that market; a rule with no
    notion_page_id means it would create a new row rather than update the one
    already in the log.
    """
    rules = tracked_config()
    rows = build_rollup(conn)
    by_rule: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(rules))}
    untracked = 0

    for row in rows:
        rule = match_tracked(row, rules)
        if rule is None:
            untracked += 1
            continue
        by_rule[rules.index(rule)].append(row)

    print(f"{len(rows)} sub-markets in the tape, {len(rules)} track rules\n")
    problems = 0
    for i, rule in enumerate(rules):
        matches = by_rule[i]
        bound = "bound to existing row" if rule["notion_page_id"] else "WOULD CREATE A NEW ROW"
        if len(matches) == 1:
            m = matches[0]
            print(f"  ok    {rule['event_slug']}  [{rule['outcome']}]")
            print(f"        -> {m.get('outcome_name')!r}  ({m.get('question')[:60]}...)")
            print(f"        -> {bound}")
            if not rule["notion_page_id"]:
                problems += 1
        elif not matches:
            print(f"  MISS  {rule['event_slug']}  [{rule['outcome']}] matched nothing")
            print(f"        (has the poller discovered this event yet?)")
            problems += 1
        else:
            print(f"  AMBIG {rule['event_slug']}  [{rule['outcome']}] matched {len(matches)}:")
            for m in matches:
                print(f"        - {m.get('outcome_name')!r}")
            problems += 1
        print()

    print(f"{untracked} sub-markets polled but not logged (expected -- full boards)")
    if problems:
        print(f"\n{problems} rule(s) need attention before syncing.")
    return problems


# --------------------------------------------------------------------------
# Page lookup / write
# --------------------------------------------------------------------------

def find_page_by_title(title: str) -> str | None:
    """Sub-markets of one event share a URL, so the question is the key."""
    if not title:
        return None
    result = _call(
        "POST",
        f"/databases/{_database_id()}/query",
        {"filter": {"property": "Market", "title": {"equals": title[:2000]}},
         "page_size": 1},
    )
    pages = result.get("results", [])
    return pages[0]["id"] if pages else None


def create_page(props: dict[str, Any]) -> str:
    return _call("POST", "/pages",
                 {"parent": {"database_id": _database_id()},
                  "properties": props})["id"]


def update_page(page_id: str, props: dict[str, Any]) -> None:
    _call("PATCH", f"/pages/{page_id}", {"properties": props})


def verify_schema() -> bool:
    """Confirm every property we intend to write exists with the type we assume.

    Cheap insurance against a renamed column turning every sync into a 400.
    """
    schema = _call("GET", f"/databases/{_database_id()}").get("properties", {})
    expected = {
        "Market": "title", "Category": "select", "Polymarket URL": "url",
        "Open Date": "date", "Resolution Date": "date",
        "Opening Probability": "number", "Mid-Period Probability": "number",
        "Closing Probability": "number", "Comparable Probability": "number",
        "Comparable Source": "select", "Actual Outcome": "select",
    }
    ok = True
    for name, ptype in expected.items():
        actual = schema.get(name, {}).get("type")
        if actual is None:
            print(f"  MISSING  {name}")
            ok = False
        elif actual != ptype:
            print(f"  TYPE     {name}: expected {ptype}, found {actual}")
            ok = False
        else:
            print(f"  ok       {name} ({ptype})")
    print(f"\nDatabase {_database_id()} is "
          f"{'reachable' if schema else 'NOT reachable -- is the integration shared into it?'}.")
    return ok


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(*, dry_run: bool = False, limit: int | None = None,
        force: bool = False) -> dict[str, int]:
    stats = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}

    db.init_db()
    with db.get_connection() as conn:
        conn.executescript(SYNC_STATE_SQL)
        rules = tracked_config()
        all_rows = build_rollup(conn)

        # Only tracked sub-markets get a Notion row. The rest of each board is
        # still in the tape for context -- syncing all of it would put ~50
        # bracket rows in a log meant to hold the handful you are observing.
        rows = []
        for row in all_rows:
            rule = match_tracked(row, rules)
            if rule is None:
                continue
            row["_track_rule"] = rule
            rows.append(row)

        skipped = len(all_rows) - len(rows)
        if limit:
            rows = rows[:limit]

        cached = {r["market_id"]: (r["notion_page_id"], r["payload_hash"])
                  for r in conn.execute(
                      "SELECT market_id, notion_page_id, payload_hash FROM notion_sync_state")}

        log.info("%d tracked of %d sub-markets (%d not logged) -> database %s",
                 len(rows), len(all_rows), skipped, _database_id())
        if not rows:
            log.warning("nothing tracked -- run --link-report to see why")

        for row in rows:
            market_id = row["market_id"]
            props = build_properties(row)
            digest = payload_hash(props)

            if dry_run:
                print(f"\n[dry-run] {row.get('question')}")
                print(f"          comparable: {row.get('comparable_series')} "
                      f"= {row.get('comparable_value')}")
                print(json.dumps(props, indent=2)[:700])
                continue

            page_id, prev_hash = cached.get(market_id, (None, None))
            if not force and prev_hash == digest:
                stats["unchanged"] += 1
                continue

            try:
                # Precedence: the page id pinned in config/markets.yml wins.
                # The seven observation rows were created by hand before this
                # pipeline existed, and their titles do not necessarily match
                # Polymarket's question text -- so a title lookup would miss
                # them and create duplicates alongside the originals.
                if not page_id:
                    page_id = (row.get("_track_rule") or {}).get("notion_page_id")
                if not page_id:
                    page_id = find_page_by_title(row.get("question") or "")
                if page_id:
                    props = protect_existing(props, existing_values(page_id))
                    digest = payload_hash(props)
                    if not force and prev_hash == digest:
                        stats["unchanged"] += 1
                        continue
                    update_page(page_id, props)
                    stats["updated"] += 1
                else:
                    page_id = create_page(props)
                    stats["created"] += 1

                conn.execute(
                    """
                    INSERT INTO notion_sync_state
                        (market_id, notion_page_id, last_synced_at, payload_hash)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        notion_page_id = excluded.notion_page_id,
                        last_synced_at = excluded.last_synced_at,
                        payload_hash   = excluded.payload_hash
                    """,
                    (market_id, page_id, db.utcnow_iso(), digest),
                )
            except NotionError as exc:
                stats["failed"] += 1
                log.error("FAILED %r: %s", row.get("question"), exc)

    if not dry_run:
        log.info("created %d, updated %d, unchanged %d, failed %d",
                 stats["created"], stats["updated"], stats["unchanged"], stats["failed"])
    if stats["failed"]:
        raise SystemExit(1)
    return stats


def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Sync the SQLite rollup into Notion.")
    p.add_argument("--dry-run", action="store_true", help="print payloads, write nothing")
    p.add_argument("--limit", type=int, help="only sync the first N markets")
    p.add_argument("--force", action="store_true", help="ignore the payload hash cache")
    p.add_argument("--verify-schema", action="store_true",
                   help="check the Notion database columns and exit")
    p.add_argument("--link-report", action="store_true",
                   help="show what each track rule in markets.yml resolved to, and exit")
    args = p.parse_args()

    if args.link_report:
        db.init_db()
        with db.get_connection() as conn:
            conn.executescript(SYNC_STATE_SQL)
            raise SystemExit(1 if link_report(conn) else 0)

    if args.verify_schema:
        raise SystemExit(0 if verify_schema() else 1)
    run(dry_run=args.dry_run, limit=args.limit, force=args.force)


if __name__ == "__main__":
    _main()
