"""Resolution logging: closed markets -> Actual Outcome, Closing Probability, P&L.

Runs after a market settles. For every tracked sub-market that Polymarket has
closed, this writes three things back to the Notion Observation Log:

    Actual Outcome        YES or NO
    Closing Probability   the last YES price we observed before settlement
    Hypothetical P&L      what a paper stake would have returned

Two deliberate restraints
-------------------------
**It never invents your position.** Hypothetical P&L only gets written when
`My Independent Read` is already filled in, because the side of the trade is
derived from your read versus the opening price. A market you never formed a
read on gets Actual Outcome and Closing Probability and an empty P&L -- which
is the honest record of what happened.

**Closing Probability comes from the tape, not from the settled price.** Once a
market resolves, Polymarket's price is 1.0 or 0.0, which tells you nothing. The
useful number is the last price observed while the outcome was still live, so
that is what gets written.

Run from repo root:
    python -m src.resolve --dry-run
    python -m src.resolve
    python -m src.resolve --market <condition_id>
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Any

from src import config, db, notion_sync
from src.poller import _get_json, GAMMA_BASE

log = logging.getLogger("resolve")

# A settled market prices at the extreme. Anything in between means Polymarket
# has flagged it closed but not yet settled, so we leave it alone.
SETTLED_HIGH = 0.99
SETTLED_LOW = 0.01


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_event_markets(slug: str) -> list[dict[str, Any]]:
    data = _get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
    event = data[0] if isinstance(data, list) and data else data
    return (event or {}).get("markets", []) or []


def settled_outcome(market: dict[str, Any]) -> str | None:
    """'YES', 'NO', or None if the market is not cleanly settled yet."""
    if not market.get("closed"):
        return None
    price = notion_sync_yes_price(market)
    if price is None:
        return None
    if price >= SETTLED_HIGH:
        return "YES"
    if price <= SETTLED_LOW:
        return "NO"
    return None


def notion_sync_yes_price(market: dict[str, Any]) -> float | None:
    """YES price from a Gamma market payload (outcomePrices is JSON-encoded)."""
    import json as _json
    outcomes, prices = market.get("outcomes"), market.get("outcomePrices")
    try:
        if isinstance(outcomes, str):
            outcomes = _json.loads(outcomes)
        if isinstance(prices, str):
            prices = _json.loads(prices)
        if outcomes and prices:
            for name, price in zip(outcomes, prices):
                if str(name).strip().lower() == "yes":
                    return _f(price)
            return _f(prices[0])
    except (ValueError, TypeError):
        pass
    return _f(market.get("lastTradePrice"))


def last_live_price(conn: sqlite3.Connection, market_id: str) -> tuple[float | None, str | None]:
    """Last YES price observed while the market was still trading.

    Skips the terminal 1.0/0.0 prints so the logged Closing Probability is the
    market's final *opinion*, not its settlement.
    """
    row = conn.execute(
        """
        SELECT yes_price, timestamp FROM price_snapshots
        WHERE market_id = ? AND yes_price IS NOT NULL
          AND yes_price > ? AND yes_price < ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (market_id, SETTLED_LOW, SETTLED_HIGH),
    ).fetchone()
    if row:
        return row["yes_price"], row["timestamp"]
    # Nothing mid-range: fall back to the last snapshot of any kind.
    row = conn.execute(
        "SELECT yes_price, timestamp FROM price_snapshots "
        "WHERE market_id = ? AND yes_price IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    return (row["yes_price"], row["timestamp"]) if row else (None, None)


def hypothetical_pnl(
    independent_read: float | None,
    entry_price: float | None,
    outcome: str,
    stake: float,
) -> tuple[float | None, str | None]:
    """Paper P&L on a fixed stake, sided by your read vs the market price.

    Returns (pnl, explanation). If there is no read, returns (None, reason) --
    the side of the trade is your judgment, and this module will not guess it.

    Convention: read above the market price means you thought YES was cheap, so
    the paper position buys YES at the observed entry price. Read below means
    the paper position buys NO at (1 - entry). Shares = stake / price, each
    settling at $1 if correct and $0 if not.
    """
    if independent_read is None:
        return None, "no independent read recorded"
    if entry_price is None or not (0.0 < entry_price < 1.0):
        return None, "no usable entry price"

    if abs(independent_read - entry_price) < 1e-9:
        return 0.0, "read matched the market exactly - no position taken"

    if independent_read > entry_price:
        side, price = "YES", entry_price
    else:
        side, price = "NO", 1.0 - entry_price
    if not (0.0 < price < 1.0):
        return None, "degenerate entry price"

    shares = stake / price
    won = (side == outcome)
    pnl = round(shares - stake, 2) if won else round(-stake, 2)
    return pnl, (
        f"${stake:.0f} on {side} at {price:.3f} "
        f"({shares:.1f} shares), settled {outcome} - {'win' if won else 'loss'}"
    )


def read_notion_row(page_id: str) -> dict[str, Any]:
    """Current Independent Read / Actual Outcome for one logged market."""
    page = notion_sync._call("GET", f"/pages/{page_id}")
    props = page.get("properties", {})

    def number(name: str) -> float | None:
        return (props.get(name) or {}).get("number")

    def select(name: str) -> str | None:
        sel = (props.get(name) or {}).get("select")
        return sel.get("name") if sel else None

    return {
        "independent_read": number("My Independent Read"),
        "opening_probability": number("Opening Probability"),
        "actual_outcome": select("Actual Outcome"),
    }


def run(*, dry_run: bool = False, only_market: str | None = None) -> dict[str, int]:
    stats = {"resolved": 0, "already": 0, "pending": 0, "skipped": 0, "failed": 0}
    stake = float(config.section("resolution").get("paper_stake", 25))
    rules = notion_sync.tracked_config()

    db.init_db()
    with db.get_connection() as conn:
        conn.executescript(notion_sync.SYNC_STATE_SQL)
        rollup = {r["market_id"]: r for r in notion_sync.build_rollup(conn)}

        # Gamma is queried once per event, not once per sub-market.
        by_slug: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            by_slug.setdefault(rule["event_slug"], [])

        gamma_cache: dict[str, list[dict[str, Any]]] = {}
        for slug in by_slug:
            try:
                gamma_cache[slug] = fetch_event_markets(slug)
            except Exception as exc:  # noqa: BLE001
                log.error("could not fetch event %s: %s", slug, exc)
                gamma_cache[slug] = []

        for market_id, row in rollup.items():
            rule = notion_sync.match_tracked(row, rules)
            if rule is None:
                continue
            if only_market and market_id != only_market:
                continue

            gamma = next(
                (m for m in gamma_cache.get(rule["event_slug"], [])
                 if str(m.get("conditionId") or m.get("id")) == market_id),
                None,
            )
            if gamma is None:
                stats["skipped"] += 1
                continue

            outcome = settled_outcome(gamma)
            if outcome is None:
                stats["pending"] += 1
                continue

            page_id = rule.get("notion_page_id") or conn.execute(
                "SELECT notion_page_id FROM notion_sync_state WHERE market_id = ?",
                (market_id,),
            ).fetchone()
            if isinstance(page_id, sqlite3.Row):
                page_id = page_id["notion_page_id"]
            if not page_id:
                log.warning("%s settled %s but has no Notion row", row.get("question"), outcome)
                stats["skipped"] += 1
                continue

            try:
                current = read_notion_row(page_id)
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                log.error("could not read Notion row for %s: %s", row.get("question"), exc)
                continue

            if current["actual_outcome"] in ("YES", "NO"):
                stats["already"] += 1
                continue

            closing, closing_ts = last_live_price(conn, market_id)
            entry = current["opening_probability"] or row.get("opening_probability")
            pnl, why = hypothetical_pnl(current["independent_read"], entry, outcome, stake)

            props: dict[str, Any] = {
                "Actual Outcome": {"select": {"name": outcome}},
                "Closing Probability": {"number": round(closing, 6) if closing is not None else None},
            }
            if pnl is not None:
                props["Hypothetical P&L"] = {"number": pnl}

            label = (row.get("question") or market_id)[:60]
            if dry_run:
                print(f"\n[dry-run] {label}")
                print(f"   settled      : {outcome}")
                print(f"   closing prob : {closing} (last live tick {closing_ts})")
                print(f"   P&L          : {pnl}  [{why}]")
                stats["resolved"] += 1
                continue

            try:
                notion_sync.update_page(page_id, props)
                db.set_market_active(conn, market_id, False)
                stats["resolved"] += 1
                log.info("%s -> %s | close %.3f | P&L %s (%s)",
                         label, outcome, closing or float("nan"),
                         pnl if pnl is not None else "-", why)
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                log.error("could not write %s: %s", label, exc)

    log.info("resolved %d, already logged %d, still open %d, skipped %d, failed %d",
             stats["resolved"], stats["already"], stats["pending"],
             stats["skipped"], stats["failed"])
    if stats["failed"]:
        raise SystemExit(1)
    return stats


def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Log resolutions back to the Notion log.")
    p.add_argument("--dry-run", action="store_true", help="show what would be written")
    p.add_argument("--market", help="only this condition_id")
    args = p.parse_args()
    run(dry_run=args.dry_run, only_market=args.market)


if __name__ == "__main__":
    _main()
