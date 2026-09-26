# Proposal: replace `cme_fedwatch` with Kalshi's public API

Status: proposal only, no code. Written after `fedwatch-daily` was disabled
(see PR disabling the schedule) because CME blocks the Actions runner at the
protocol level (`net::ERR_HTTP2_PROTOCOL_ERROR` on `page.goto`) — a site-side
block, not a scraper bug. Per Phase 1 discipline, we are not going to fight
that block with user-agents, proxies, or stealth flags. This proposes a
different, publicly-documented comparable instead: Kalshi's FOMC rate-decision
markets, which are observation data of the same kind (a market-implied
probability), just from a second venue instead of a derivative-pricing tool.

## Why Kalshi is a fair substitute for FedWatch here

FedWatch turns CME fed funds futures prices into implied CUT/HOLD/HIKE and
target-range-bucket probabilities. Kalshi's FOMC markets are *themselves* a
market pricing the same event, expressed directly as a probability (no
futures-to-probability derivation needed). It is not a perfect substitute —
Kalshi is a different pool of participants/liquidity than CME fed funds
futures — but it is a second independent observation of the market's view on
the same FOMC meetings, which is what the comparables table already holds one
of (FRED is a third: the actual current rate, not a market view). No auth is
required for market data.

## Endpoints

Base: `https://api.elections.kalshi.com/trade-api/v2` (Kalshi's public,
no-auth market-data API; confirmed reachable directly with `curl`, no
blocking observed).

1. `GET /events?series_ticker=KXFEDDECISION&status=open`
   Lists each upcoming FOMC meeting as one "event." Confirmed live response
   includes fields we need per event: `event_ticker` (e.g.
   `KXFEDDECISION-26OCT`), `strike_date` (e.g. `2026-10-28T18:00:00Z` — the
   meeting's decision timestamp), `title` (e.g. `"Fed decision in Oct 2026?"`).

2. `GET /markets?event_ticker=<event_ticker>`
   Lists each rate-decision bucket ("market") within that meeting as a binary
   yes/no contract. Confirmed live response for `KXFEDDECISION-26OCT` returns
   5 markets:
   - `KXFEDDECISION-26OCT-C26` — "Cut >25bps"
   - `KXFEDDECISION-26OCT-C25` — "Cut 25bps"
   - `KXFEDDECISION-26OCT-H0`  — "Hike 0bps" (i.e. hold)
   - `KXFEDDECISION-26OCT-H25` — "Hike 25bps"
   - `KXFEDDECISION-26OCT-H26` — "Hike >25bps"

   Each market carries `yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`
   (strings, dollars not cents — e.g. `"0.6400"` = 64%), and `subtitle` /
   `yes_sub_title` giving the bucket label in plain English. `last_price_dollars`
   is the natural probability read (mid of bid/ask would also work; last-price
   matches how FedWatch's own published number is a single point read, not a
   spread).

No API key needed for either endpoint — both were hit anonymously above and
returned real data.

## Which markets map to which FOMC meetings

`config/events.yml` already carries the FOMC calendar
(`date: 2026-10-28`, `date: 2026-12-09`, ...). Kalshi's `event_ticker` suffix
is `<2-digit-year><MON>` of the meeting month (`26OCT`, `26DEC`, `27JAN`, ...),
and the returned `strike_date` gives the exact meeting date/time — so the
match is: build `event_ticker` candidates from each `events.yml` FOMC date's
year+month and confirm against `strike_date`, rather than trusting the ticker
format to never drift. Confirmed live:

| events.yml date | Kalshi event_ticker | Kalshi strike_date |
|---|---|---|
| 2026-10-28 | `KXFEDDECISION-26OCT` | `2026-10-28T18:00:00Z` |
| 2026-12-09 | `KXFEDDECISION-26DEC` | `2026-12-09T19:00:00Z` |

(Sep 16 2026 meeting has presumably already closed/settled by the time this
lands — closed events still return from `/events` with `status=closed` if
history is needed, e.g. for backfill or Brier scoring against the settled
outcome.)

Within a meeting, five Kalshi markets collapse onto FedWatch's existing
vocabulary:

| Kalshi market subtitle | Direction (matches `outcome_direction_map` in config/comparables.yml) | Bucket vs. current target range |
|---|---|---|
| Hike >25bps | HIKE | current_upper + 50bps or more |
| Hike 25bps  | HIKE | current_upper + 25bps |
| Hike 0bps   | HOLD | unchanged |
| Cut 25bps   | CUT  | current_lower - 25bps |
| Cut >25bps  | CUT  | current_lower - 50bps or more |

This is coarser than FedWatch's full bucket grid (FedWatch publishes several
25bp-wide target-range buckets covering a wider spread; Kalshi's FOMC markets
are structured as "how big a move, in which direction, at this meeting," five
buckets total). That's a real fidelity loss for the rate-*level* comparables
(`FOMC:<date>:<range>`, e.g. `FOMC:2026-12-09:350-375`) used by
`level_bucket_for()` — Kalshi does not publish an outright "3.50-3.75% at
December" contract, only "cut 25bps at December" relative to whatever the
range is *going into* that meeting. Reconstructing an absolute bucket
probability from Kalshi would require chaining meeting-over-meeting cut/hike
odds (this meeting's HOLD/CUT/HIKE from `comparables` combined with the prior
meeting's resolved level), which is a real modeling step, not a straight
re-source — flagged here rather than silently done, since Phase 1 forbids
adding derived/interpretive series without saying so.
For the **direction** comparables (`FOMC:<date>:HOLD`/`CUT`/`HIKE` — the
"no change" / "cut" / "hike" outcome markets, which is most of what
`comparable_series_for()` currently matches) Kalshi maps directly and loses
nothing: sum `Cut 25bps` + `Cut >25bps` prices for CUT, `Hike 0bps` for HOLD,
`Hike 25bps` + `Hike >25bps` for HIKE.

## How rows would land in the `comparables` table

Same shape FedWatch already writes, new `source` value so history stays
separable and nothing already on the tape is touched:

```
source = "kalshi_fedwatch"
series_id = "FOMC:<meeting_date>:HOLD"   -- e.g. FOMC:2026-10-28:HOLD
series_id = "FOMC:<meeting_date>:CUT"
series_id = "FOMC:<meeting_date>:HIKE"
value = summed yes-price for that direction, 0.0-1.0
timestamp = scrape time, midnight UTC of the pull date (same convention as fred.py / fedwatch.py, so a daily job doesn't duplicate)
notes = raw per-bucket prices as JSON, e.g. {"C25": 0.01, "C26": 0.01, "H0": 0.36, "H25": 0.64, "H26": 0.02}
   (mirrors how fedwatch.py's persist() already stashes a disagreement/basis
   note — keeps the underlying bucket prices auditable without a second table)
```

`(source, series_id, timestamp)` uniqueness check + update-in-place, exactly
like `fred.save_point()` / `fedwatch._save()` already do — no new pattern
needed.

`src/notion_sync.py`'s `SOURCE_LABELS` and `VALID_SOURCES` would need
`"kalshi_fedwatch": "Kalshi"` added (a new Notion "Comparable Source" select
option, additive, doesn't touch existing FedWatch rows) if/when this replaces
FedWatch as what a meeting-outcome sub-market links to. `comparable_series_for()`
itself wouldn't need to change shape — it already returns `(source,
series_id)`, just the source string flips.

Rate-*level* sub-markets (`level_bucket_for()`) would keep pointing at
`cme_fedwatch` (or go comparable-less) until/unless the meeting-chaining
question above gets a real answer — not silently reinterpreted through the
new source.

## Test plan

Unit tests (no network, mirrors `tests/test_fedwatch.py`'s existing style of
testing normalisers/parsers against saved fixtures):

1. `test_event_ticker_for_meeting` — given an `events.yml`-shaped date, builds
   the candidate `event_ticker` and asserts it matches a saved sample
   `strike_date` fixture (use the two live responses captured above as
   fixtures, same pattern as `tests/fixtures/fedwatch_*.html`).
2. `test_direction_probabilities_from_markets` — given a saved 5-market JSON
   fixture (the actual `KXFEDDECISION-26OCT` response above, saved verbatim),
   asserts CUT/HOLD/HIKE sum correctly and sum to ≈1.0 (bid/ask spread means
   not exactly 1.0 — assert within a tolerance, same spirit as
   `resolve_directions()`'s existing `tolerance=0.005` disagreement check).
3. `test_missing_bucket_handled` — a market list missing one subtitle (e.g. a
   meeting where "Cut >25bps" was delisted early) still produces a value for
   the other two directions rather than raising.
4. `test_persist_uses_new_source` — `_save()`-equivalent writes
   `source="kalshi_fedwatch"`, doesn't touch any existing `cme_fedwatch` row,
   and re-running the same day's pull updates in place instead of duplicating
   (copy `fred.py`'s `save_point` test pattern).

Integration / dry-run test plan (once code exists, before any workflow
schedule is turned on):

1. `python -m src.kalshi_fedwatch --dry-run` against the live API, confirm it
   logs the expected rows for every open FOMC event without writing.
2. Run once for real on a `fix/` branch via `workflow_dispatch` with
   `dry_run=true` (matching the existing pattern other workflows in this repo
   use for pre-merge verification), inspect the printed rows against
   `docs/kalshi-fedwatch-replacement-proposal.md`'s worked Oct 2026 example
   above by hand.
3. `python -m src.divergence --report` after one real (non-dry) write, confirm
   divergence rows appear for Fed sub-markets in tape order (unsorted, per
   Phase 1) with `comparable_source = kalshi_fedwatch`.
4. Leave `cme_fedwatch` rows and the disabled `fedwatch-daily` workflow alone
   during this — this is additive until KeAndre decides to fully retire
   FedWatch, not a replace-in-place.

## What this proposal does NOT do

- No ranking, filtering, or thresholding of Kalshi vs. Polymarket divergence —
  same discipline as the existing `src/divergence.py`.
- No trading, no Kalshi auth/API-key setup (market data endpoints above are
  public/no-auth) — this stays read-only observation.
- No code changes yet. Next step if approved: a `fix/` (or `feat/`) branch
  adding `src/kalshi_fedwatch.py` mirroring `src/fred.py`'s structure, plus
  the `SOURCE_LABELS`/`VALID_SOURCES` addition and the tests above.
