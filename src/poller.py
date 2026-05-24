"""Polymarket poller.

Two steps per run:

  1. discover()  -- for each event in config/markets.yml, ensure its
                    sub-markets exist in the SQLite `markets` table.
                    Idempotent: safe to call on every run.

  2. poll()      -- for each active market, hit Polymarket's CLOB midpoint
                    endpoint and write a row to `price_snapshots`.

This module is INGESTION ONLY. It writes raw observations to SQLite.
It does NOT compute edge, rank markets, or generate signals -- that's
phase 3 territory.

Run from repo root:
    python -m src.poller          # discover + poll
    python -m src.poller poll     # poll only (skip discovery)
    python -m src.poller discover # discovery only (no polling)
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from src import db

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

MARKETS_CONFIG = Path(__file__).resolve().parent.parent / "config" / "markets.yml"

REQUEST_TIMEOUT = 15  # seconds
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.5  # seconds, doubles on each retry

log = logging.getLogger("poller")


# ---------- HTTP helpers ----------

def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET with retries. Returns parsed JSON or raises on final failure."""
    last_exc: Exception | None = None
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 -- want all network/parse errors
            last_exc = exc
            log.warning("GET %s attempt %d/%d failed: %s", url, attempt, RETRY_ATTEMPTS, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc


# ---------- Config loading ----------

def load_market_config(path: Path = MARKETS_CONFIG) -> list[dict[str, Any]]:
    """Return the list of event entries from config/markets.yml."""
    with open(path) as f:
        data = yaml.safe_load(f)
    events = data.get("events", []) if data else []
    return [e for e in events if e.get("active", True)]


# ---------- Polymarket Gamma API ----------

def fetch_event_by_slug(slug: str) -> dict[str, Any]:
    """Fetch a Polymarket event (with its sub-markets) by slug.

    Returns the event dict. Raises if the slug isn't found.
    """
    data = _get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
    if isinstance(data, list):
        if not data:
            raise ValueError(f"No event found for slug={slug!r}")
        return data[0]
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unexpected Gamma response shape for slug={slug!r}: {type(data)}")


def _parse_token_ids(raw: Any) -> list[str]:
    """clobTokenIds may come back as a JSON-encoded string list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except json.JSONDecodeError:
            pass
    return []


# ---------- Discovery ----------

def discover() -> None:
    """Walk config and ensure every sub-market exists in SQLite."""
    config_events = load_market_config()
    db.init_db()
    with db.get_connection() as conn:
        for cfg in config_events:
            slug = cfg["slug"]
            log.info("discovering event slug=%s", slug)
            try:
                event = fetch_event_by_slug(slug)
            except Exception as exc:  # noqa: BLE001
                log.error("failed to fetch event %s: %s", slug, exc)
                continue

            sub_markets = event.get("markets", []) or []
            log.info("  event %s has %d sub-markets", slug, len(sub_markets))

            for m in sub_markets:
                condition_id = m.get("conditionId") or m.get("id")
                token_ids = _parse_token_ids(m.get("clobTokenIds"))
                if not condition_id or not token_ids:
                    log.warning("  skipping sub-market without conditionId or tokens: %s", m.get("question"))
                    continue

                # Prefer groupItemTitle ("No change", "25 bps decrease") -- that's the
                # human-readable bucket. The outcomes array is just ["Yes", "No"] for the
                # binary sub-market, which is useless as a label.
                outcome_name = m.get("groupItemTitle")
                if not outcome_name:
                    outcomes = m.get("outcomes")
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except json.JSONDecodeError:
                            outcomes = []
                    if outcomes and outcomes[0] not in ("Yes", "No"):
                        outcome_name = outcomes[0]

                db.upsert_market(
                    conn,
                    market_id=str(condition_id),
                    event_slug=slug,
                    event_name=cfg["name"],
                    category=cfg["category"],
                    question=m.get("question", ""),
                    outcome_name=outcome_name,
                    yes_token_id=token_ids[0],
                    no_token_id=token_ids[1] if len(token_ids) > 1 else None,
                    polymarket_url=f"https://polymarket.com/event/{slug}",
                    resolution_date=cfg.get("resolution_date"),
                )


# ---------- Polling ----------

def _fetch_midpoint(token_id: str) -> float | None:
    """Fetch CLOB midpoint for a token. Returns None on failure."""
    try:
        data = _get_json(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("midpoint fetch failed for token %s: %s", token_id, exc)
        return None
    mid = data.get("mid") if isinstance(data, dict) else None
    if mid is None:
        return None
    try:
        return float(mid)
    except (TypeError, ValueError):
        return None


def _fetch_book_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    """Fetch best bid/ask from CLOB order book. Returns (bid, ask) or (None, None)."""
    try:
        data = _get_json(f"{CLOB_BASE}/book", params={"token_id": token_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("book fetch failed for token %s: %s", token_id, exc)
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    # Polymarket order book: bids sorted desc, asks sorted asc -- but be defensive.
    def _best(side: list, *, highest: bool) -> float | None:
        prices = []
        for level in side:
            try:
                prices.append(float(level.get("price")))
            except (TypeError, ValueError, AttributeError):
                continue
        if not prices:
            return None
        return max(prices) if highest else min(prices)
    return (_best(bids, highest=True), _best(asks, highest=False))


def poll(mode: str = "routine") -> int:
    """Poll all active markets, write snapshots. Returns count of snapshots written."""
    db.init_db()
    written = 0
    with db.get_connection() as conn:
        markets = db.get_active_markets(conn)
        log.info("polling %d active market(s) in mode=%s", len(markets), mode)
        for m in markets:
            mid = _fetch_midpoint(m["yes_token_id"])
            if mid is None:
                log.warning("no midpoint for %s (%s) -- skipping snapshot", m["market_id"], m["question"])
                continue
            bid, ask = _fetch_book_bid_ask(m["yes_token_id"])
            db.save_price_snapshot(
                conn,
                market_id=m["market_id"],
                yes_price=mid,
                no_price=(1.0 - mid) if mid is not None else None,
                yes_bid=bid,
                yes_ask=ask,
                mode=mode,
            )
            written += 1
            log.info("  %s: yes=%.3f bid=%s ask=%s", m["market_id"][:12], mid, bid, ask)
    log.info("wrote %d snapshot(s)", written)
    return written


# ---------- CLI ----------

def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("all", "discover"):
        discover()
    if cmd in ("all", "poll"):
        poll(mode="routine")
    if cmd not in ("all", "discover", "poll"):
        print("usage: python -m src.poller [all|discover|poll]")
        sys.exit(1)


if __name__ == "__main__":
    _main()
