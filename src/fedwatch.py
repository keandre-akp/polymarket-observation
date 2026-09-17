"""CME FedWatch scraper -> the `comparables` table (source = "cme_fedwatch").

Everything here is shaped by three things confirmed against the live tool in a
real browser on 2026-08-05:

1. **You cannot go straight at the QuikStrike URL.** It runs a referrer check
   and answers a direct hit with "Access to QuikStrike has been denied -
   unexpected null referrer". So we load the cmegroup.com page and work inside
   the iframe.

2. **The tool is below the fold and lazy-loaded.** The iframe does not start
   fetching until it scrolls into view.

3. **The default view is not the grid you would expect.** The "Current" view
   shows ONE meeting at a time with target-rate ranges as ROW labels and time
   offsets (NOW / 1 DAY / 1 WEEK / 1 MONTH) as columns -- the transpose of the
   classic meeting-by-range grid. It also renders a separate EASE / NO CHANGE /
   HIKE summary and a tab strip of the next ~10 FOMC meetings.

So the scraper walks the tab strip, and the parser handles three shapes:

  A. meeting-per-row x range-per-column   (the "Probabilities" nav view)
  B. range-per-row x time-offset-per-column, taking NOW  (the default view)
  C. the EASE / NO CHANGE / HIKE summary

No CSS selectors are pinned: QuikStrike is an ASP.NET app with generated ids
like `ctl00_MainContent_ucViewControl_...` that move between deploys. Tables
are found structurally instead.

Raw HTML of every frame is written to data/fedwatch_raw/ *before* any parsing,
so a parser failure is never a data loss. That directory is already gitignored,
so snapshots stay local; CI uploads them as build artifacts.

Series written:
    FOMC:2026-09-16:350-375        probability of that bucket
    FOMC:2026-09-16:HOLD/CUT/HIKE  direction probabilities

CME also publishes an official FedWatch REST API through dataservices.cmegroup.com.
If access comes through, retire this module -- a supported endpoint beats even a
careful scraper.

Run from repo root:
    python -m src.fedwatch
    python -m src.fedwatch --headed
    python -m src.fedwatch --from-file data/fedwatch_raw/x.html
    python -m src.fedwatch --dump-tables
    python -m src.fedwatch --reparse-all
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup

from src import config, db

SOURCE = "cme_fedwatch"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

log = logging.getLogger("fedwatch")

# "375-400", "3.75-4.00", "375 - 400", "375–400" (en dash)
RANGE_RE = re.compile(
    r"^\s*(\d{1,4}(?:\.\d{1,2})?)\s*[-–—]\s*(\d{1,4}(?:\.\d{1,2})?)\s*$"
)
PERCENT_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%\s*$")
# The live tool labels the standing range "350-375 (Current)".
PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*")
NOW_RE = re.compile(r"^\s*NOW\b", re.I)
TIME_OFFSET_RE = re.compile(r"^\s*(NOW|1\s*DAY|1\s*WEEK|1\s*MONTH)\b", re.I)
CURRENT_RANGE_RE = re.compile(
    r"current\s+target\s+rate\s+is\s*(\d{2,4}\s*[-–—]\s*\d{2,4})", re.I
)
CAPTURE_SPLIT_RE = re.compile(r"<!--\s*capture:", re.I)
DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})"
    r"|(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"|(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
    r"|([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
)
DIRECTIONS = {"EASE": "CUT", "NO CHANGE": "HOLD", "HIKE": "HIKE"}


# --------------------------------------------------------------------------
# Normalisers (pure -- unit-testable without a browser)
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).replace("\xa0", " ").strip()


def normalise_range(token: str) -> str | None:
    """'3.75-4.00', '375-400' and '350-375 (Current)' -> basis-point range.

    The parenthetical matters: the live tool marks the standing range as
    '350-375 (Current)', and an exact-match regex would silently drop the
    single most important row on the page.
    """
    text = PARENTHETICAL_RE.sub("", _clean(token))
    m = RANGE_RE.match(text)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    # Values under 25 are percent, not basis points.
    if lo < 25 and hi <= 25:
        lo, hi = lo * 100, hi * 100
    if hi <= lo:
        return None
    return f"{int(round(lo))}-{int(round(hi))}"


def normalise_percent(token: str) -> float | None:
    """'62.4%' -> 0.624. None for anything that is not a percentage."""
    m = PERCENT_RE.match(_clean(token))
    if not m:
        return None
    value = float(m.group(1)) / 100.0
    return value if 0.0 <= value <= 1.0 else None


def normalise_meeting_date(token: str) -> str | None:
    """'16 Sep 2026' -> '2026-09-16'."""
    text = _clean(token)
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Table parsing
# --------------------------------------------------------------------------

def _table_to_grid(table) -> list[list[str]]:
    grid: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ")) for td in tr.find_all(["th", "td"])]
        if cells:
            grid.append(cells)
    return grid


def score_table(grid: list[list[str]]) -> tuple[int, int]:
    """(header_row_index, score) for the wide layout. Score 0 means not it."""
    best = (-1, 0)
    for i, row in enumerate(grid[: min(6, len(grid))]):
        range_headers = sum(1 for c in row if normalise_range(c))
        if range_headers < 2:
            continue
        percent_cells = sum(
            1 for r in grid[i + 1:] for c in r if normalise_percent(c) is not None
        )
        score = range_headers * max(percent_cells, 1)
        if score > best[1]:
            best = (i, score)
    return best


def parse_wide_grid(html: str) -> list[dict[str, Any]]:
    """Strategy A: meeting-per-row x range-per-column."""
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[int, list[list[str]], int]] = []
    for table in soup.find_all("table"):
        grid = _table_to_grid(table)
        if len(grid) < 2:
            continue
        header_idx, score = score_table(grid)
        if header_idx >= 0 and score > 0:
            candidates.append((score, grid, header_idx))
    if not candidates:
        return []

    _, grid, header_idx = max(candidates, key=lambda t: t[0])
    columns = {
        idx: normalise_range(cell)
        for idx, cell in enumerate(grid[header_idx])
        if normalise_range(cell)
    }

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in grid[header_idx + 1:]:
        if not row:
            continue
        meeting = normalise_meeting_date(row[0])
        if not meeting:
            continue
        for idx, target_range in columns.items():
            if idx >= len(row):
                continue
            prob = normalise_percent(row[idx])
            if prob is None:
                continue
            key = (meeting, target_range)
            if key in seen:
                continue
            seen.add(key)
            results.append({"meeting_date": meeting, "target_range": target_range,
                            "probability": prob})
    return results


def find_meeting_date(soup) -> str | None:
    """Meeting date from the MEETING INFORMATION table, or the chart title."""
    for table in soup.find_all("table"):
        grid = _table_to_grid(table)
        for i, row in enumerate(grid):
            labels = [c.upper() for c in row]
            if "MEETING DATE" not in labels:
                continue
            col = labels.index("MEETING DATE")
            for value_row in grid[i + 1:]:
                if col < len(value_row):
                    parsed = normalise_meeting_date(value_row[col])
                    if parsed:
                        return parsed
    text = _clean(soup.get_text(" "))
    m = re.search(r"for\s+(.{3,20}?)\s+Fed\s+Meeting", text, re.I)
    return normalise_meeting_date(m.group(1)) if m else None


def parse_tall_grid(html: str, meeting_date: str | None = None) -> list[dict[str, Any]]:
    """Strategy B: range-per-row, time-offset-per-column (the default view).

    Columns are NOW / 1 DAY / 1 WEEK / 1 MONTH -- history for a single meeting,
    not other meetings. Take NOW, and the meeting date from the same block.
    """
    soup = BeautifulSoup(html, "lxml")
    meeting = meeting_date or find_meeting_date(soup)
    if not meeting:
        return []

    best: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        grid = _table_to_grid(table)
        if len(grid) < 2:
            continue

        now_col: int | None = None
        for row in grid[: min(4, len(grid))]:
            offsets = [i for i, c in enumerate(row) if TIME_OFFSET_RE.match(c)]
            if not offsets:
                continue
            now_hits = [i for i in offsets if NOW_RE.match(row[i])]
            candidate = (now_hits or offsets)[0]
            # Header rows here omit the row-label cell (rowspan), so the column
            # index shifts by however many leading cells are missing.
            widest = len(grid[-1])
            now_col = candidate + (widest - len(row) if widest > len(row) else 0)
            break

        rows: list[dict[str, Any]] = []
        for row in grid:
            if not row:
                continue
            target_range = normalise_range(row[0])
            if not target_range:
                continue
            col = now_col if now_col is not None and now_col < len(row) else None
            prob = normalise_percent(row[col]) if col is not None else None
            if prob is None:
                prob = next(
                    (p for p in (normalise_percent(c) for c in row[1:]) if p is not None),
                    None,
                )
            if prob is None:
                continue
            rows.append({"meeting_date": meeting, "target_range": target_range,
                         "probability": prob})
        if len(rows) > len(best):
            best = rows
    return best


def parse_direction_summary(html: str) -> dict[str, float]:
    """Strategy C: the EASE / NO CHANGE / HIKE table -> {CUT, HOLD, HIKE}."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        grid = _table_to_grid(table)
        for i, row in enumerate(grid):
            labels = [c.upper().strip() for c in row]
            if not {"EASE", "NO CHANGE", "HIKE"} <= set(labels):
                continue
            for value_row in grid[i + 1:]:
                out: dict[str, float] = {}
                for label, direction in DIRECTIONS.items():
                    if label not in labels:
                        continue
                    col = labels.index(label)
                    if col < len(value_row):
                        prob = normalise_percent(value_row[col])
                        if prob is not None:
                            out[direction] = prob
                if out:
                    return out
    return {}


def find_current_range(html: str) -> str | None:
    """The tool states its own reference point: 'Current target rate is 350-375'.

    Preferred over inferring it from FRED, because it is the range the displayed
    probabilities were actually computed against.
    """
    soup = BeautifulSoup(html, "lxml")
    m = CURRENT_RANGE_RE.search(_clean(soup.get_text(" ")))
    if m:
        found = normalise_range(m.group(1))
        if found:
            return found
    for table in soup.find_all("table"):
        for row in _table_to_grid(table):
            for cell in row:
                if "(current)" in cell.lower():
                    found = normalise_range(cell)
                    if found:
                        return found
    return None


def split_captures(html: str) -> list[str]:
    """Split a snapshot into per-meeting capture blocks.

    Blocks with no table are dropped -- a preamble, or the outer cmegroup.com
    frame when it carries no data, would otherwise parse as an empty meeting.
    """
    parts = [p for p in CAPTURE_SPLIT_RE.split(html) if "<table" in p.lower()]
    return parts if len(parts) > 1 else [html]


def parse_probability_grid(html: str) -> list[dict[str, Any]]:
    """[{meeting_date, target_range, probability}] from a snapshot."""
    wide = parse_wide_grid(html)
    if wide:
        return wide

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for block in split_captures(html):
        for row in parse_tall_grid(block):
            key = (row["meeting_date"], row["target_range"])
            if key in seen:
                continue
            seen.add(key)
            results.append(row)
    return results


def parse_directions(html: str) -> dict[str, dict[str, float]]:
    """{meeting_date: {CUT/HOLD/HIKE: probability}} straight from the tool."""
    out: dict[str, dict[str, float]] = {}
    for block in split_captures(html):
        soup = BeautifulSoup(block, "lxml")
        meeting = find_meeting_date(soup)
        if not meeting:
            continue
        summary = parse_direction_summary(block)
        if summary:
            out[meeting] = summary
    return out


def dump_tables(html: str, limit: int = 12) -> None:
    """Diagnostic: what tables exist, for when the parse comes back empty."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    print(f"{len(tables)} <table> elements found")
    print(f"current target range detected: {find_current_range(html)}")
    print(f"meeting date detected: {find_meeting_date(soup)}\n")
    for i, table in enumerate(tables[:limit]):
        grid = _table_to_grid(table)
        header_idx, score = score_table(grid)
        ident = table.get("id") or table.get("class") or "(no id/class)"
        print(f"--- table[{i}] id/class={ident} rows={len(grid)} wide-score={score}")
        for row in grid[:4]:
            print("      " + " | ".join(row[:10]))
        print()


# --------------------------------------------------------------------------
# Direction collapse
# --------------------------------------------------------------------------

def collapse_directions(
    rows: Iterable[dict[str, Any]], current_range: str
) -> dict[str, dict[str, float]]:
    """Grid -> {meeting: {CUT, HOLD, HIKE}} by comparing each bucket to today."""
    cur_lo = int(current_range.split("-")[0])
    buckets: dict[str, dict[str, float]] = {}
    for r in rows:
        lo = int(r["target_range"].split("-")[0])
        direction = "HOLD" if lo == cur_lo else ("CUT" if lo < cur_lo else "HIKE")
        meeting = buckets.setdefault(
            r["meeting_date"], {"CUT": 0.0, "HOLD": 0.0, "HIKE": 0.0}
        )
        meeting[direction] += r["probability"]
    return buckets


def resolve_directions(
    rows: Iterable[dict[str, Any]],
    current_range: str | None,
    published: dict[str, dict[str, float]] | None = None,
    tolerance: float = 0.005,
) -> dict[str, dict[str, Any]]:
    """{meeting: {values, basis, disagreement}}.

    The tool publishes EASE / NO CHANGE / HIKE itself. When we have those we
    use them verbatim and treat our own collapse of the bucket grid as a check:
    a gap beyond `tolerance` means one of the two parses is wrong, and it gets
    recorded rather than averaged away.
    """
    published = published or {}
    derived = collapse_directions(rows, current_range) if current_range else {}
    out: dict[str, dict[str, Any]] = {}

    for meeting in sorted(set(published) | set(derived)):
        pub, drv = published.get(meeting), derived.get(meeting)
        chosen = pub or drv
        if chosen is None:
            continue
        disagreement = None
        if pub and drv:
            gaps = {d: round(abs(pub.get(d, 0.0) - drv.get(d, 0.0)), 6)
                    for d in ("CUT", "HOLD", "HIKE")}
            if max(gaps.values()) > tolerance:
                disagreement = gaps
                log.warning("%s published vs derived directions differ: %s", meeting, gaps)
        out[meeting] = {
            "values": chosen,
            "basis": "published" if pub else "derived",
            "disagreement": disagreement,
        }
    return out


# --------------------------------------------------------------------------
# Browser capture
# --------------------------------------------------------------------------

DISMISS_SELECTORS = (
    "#onetrust-reject-all-handler",          # CME's OneTrust cookie banner
    "button:has-text('Reject All')",
    "[aria-label='Close']",
    ".cmp-modal__close",
)


def _dismiss_overlays(page) -> None:
    """Best effort. These overlay the page but do not block DOM reads."""
    for selector in DISMISS_SELECTORS:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1500):
                el.click(timeout=2000)
                page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            continue


def _wait_for_percentages(page, timeout_ms: int, poll_ms: int = 500) -> bool:
    """True once any frame's text contains a percentage.

    Polls every frame rather than waiting on the main document: the grid lives
    in a child frame, so a main-frame-only probe burns the whole timeout.
    """
    import time as _time

    probe = "() => /\\d+(\\.\\d+)?\\s*%/.test(document.body ? document.body.innerText : '')"
    deadline = _time.monotonic() + timeout_ms / 1000.0
    while _time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.evaluate(probe):
                    return True
            except Exception:  # noqa: BLE001 -- detached / navigating
                continue
        page.wait_for_timeout(poll_ms)
    return False


def _tool_frame(page):
    for frame in page.frames:
        if "quikstrike" in (frame.url or "").lower():
            return frame
    return None


def _capture_frames(page, label: str) -> list[str]:
    chunks = [f"<!-- capture: {label} frame=main url={page.url} -->\n{page.content()}"]
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            chunks.append(f"<!-- capture: {label} frame={frame.url} -->\n{frame.content()}")
        except Exception as exc:  # noqa: BLE001
            log.debug("skipped frame %s: %s", frame.url, exc)
    return chunks


def capture_snapshot(*, headed: bool = False, url: str | None = None) -> tuple[str, Path]:
    """Load FedWatch in a real browser; return (html, snapshot_path)."""
    from playwright.sync_api import sync_playwright

    cfg = config.section("fedwatch")
    target = url or cfg["page_url"]
    timeout_ms = cfg.get("timeout_ms", 60000)
    raw_dir = config.resolve_path(cfg.get("raw_dir", "data/fedwatch_raw"))
    raw_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    snapshot_path = raw_dir / f"fedwatch_{stamp}.html"

    chunks: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1600, "height": 1200},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)

        _dismiss_overlays(page)

        # Scroll the tool into view so the lazy iframe actually loads.
        for _ in range(6):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(400)

        if not _wait_for_percentages(page, timeout_ms):
            log.warning("no percentage text appeared in any frame; saving snapshot anyway")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

        frame = _tool_frame(page)
        if frame is None:
            log.warning("no QuikStrike frame found; capturing the page as-is")
            chunks.extend(_capture_frames(page, 'meeting-tab="(unknown)"'))
        else:
            tabs = frame.locator("a:visible, li:visible > a, .cmeNavItem:visible").filter(
                has_text=re.compile(r"^\s*\d{1,2}\s*[A-Za-z]{3}\s*\d{2}\s*$")
            )
            try:
                count = min(tabs.count(), cfg.get("max_meetings", 10))
            except Exception:  # noqa: BLE001
                count = 0

            if count == 0:
                log.info("no meeting tabs found; capturing the default view only")
                chunks.extend(_capture_frames(page, 'meeting-tab="(default)"'))
            else:
                log.info("%d meeting tabs to walk", count)
                for i in range(count):
                    label = "(unknown)"
                    try:
                        tab = tabs.nth(i)
                        label = _clean(tab.inner_text())
                        if i > 0:  # the first tab is already selected
                            tab.click(timeout=10000)
                            frame.wait_for_timeout(1200)
                            _wait_for_percentages(page, 15000)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("tab %d (%s) failed: %s", i, label, exc)
                        continue
                    chunks.extend(_capture_frames(page, f'meeting-tab="{label}"'))

        context.close()
        browser.close()

    html = "\n".join(chunks)
    snapshot_path.write_text(html, encoding="utf-8")
    log.info("snapshot saved: %s (%.0f KB)",
             snapshot_path.name, snapshot_path.stat().st_size / 1024)
    return html, snapshot_path


def prune_snapshots() -> int:
    """Delete raw HTML older than the retention window. 0 = keep forever."""
    cfg = config.section("fedwatch")
    days = cfg.get("raw_retention_days", 180)
    raw_dir = config.resolve_path(cfg.get("raw_dir", "data/fedwatch_raw"))
    if days <= 0 or not raw_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for path in raw_dir.glob("fedwatch_*.html"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _save(conn, series_id: str, value: float, timestamp: str, notes: str | None) -> str:
    """Insert, or update in place when the same series/timestamp is re-scraped.

    Timestamps are stamped at midnight UTC of the scrape date, so re-running
    the daily job does not append a second row for the same day.
    """
    existing = conn.execute(
        "SELECT comp_id, value FROM comparables "
        "WHERE source = ? AND series_id = ? AND timestamp = ?",
        (SOURCE, series_id, timestamp),
    ).fetchone()
    if existing is None:
        db.save_comparable(conn, source=SOURCE, series_id=series_id,
                           value=value, timestamp=timestamp, notes=notes)
        return "inserted"
    if abs(existing["value"] - value) < 1e-9:
        return "unchanged"
    conn.execute("UPDATE comparables SET value = ?, notes = ? WHERE comp_id = ?",
                 (value, notes, existing["comp_id"]))
    return "updated"


def persist(
    rows: list[dict[str, Any]],
    directions: dict[str, dict[str, Any]],
    *,
    scrape_date: str,
    snapshot_path: Path | None,
    current_range: str | None,
) -> dict[str, int]:
    timestamp = f"{scrape_date}T00:00:00Z"
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    snapshot_note = snapshot_path.name if snapshot_path else None

    db.init_db()
    with db.get_connection() as conn:
        for r in rows:
            series_id = f"FOMC:{r['meeting_date']}:{r['target_range']}"
            stats[_save(conn, series_id, r["probability"], timestamp, snapshot_note)] += 1

        for meeting, info in directions.items():
            note = json.dumps(
                {
                    "basis": info["basis"],
                    "current_target_range": current_range,
                    **({"disagreement": info["disagreement"]} if info["disagreement"] else {}),
                },
                separators=(",", ":"),
            )
            for direction in ("HOLD", "CUT", "HIKE"):
                series_id = f"FOMC:{meeting}:{direction}"
                value = round(info["values"].get(direction, 0.0), 6)
                stats[_save(conn, series_id, value, timestamp, note)] += 1
    return stats


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(
    *,
    from_file: Path | None = None,
    headed: bool = False,
    dry_run: bool = False,
    dump: bool = False,
) -> int:
    scrape_date = datetime.now(timezone.utc).date().isoformat()
    snapshot_path: Path | None = None

    if from_file:
        snapshot_path = Path(from_file)
        html = snapshot_path.read_text(encoding="utf-8", errors="replace")
        log.info("reparsing %s", snapshot_path.name)
        stamp = re.search(r"(\d{4}-\d{2}-\d{2})", snapshot_path.name)
        if stamp:
            scrape_date = stamp.group(1)
    else:
        html, snapshot_path = capture_snapshot(headed=headed)

    if dump:
        dump_tables(html)
        return 0

    rows = parse_probability_grid(html)
    if not rows:
        log.error(
            "PARSE FAILED: no probability grid found. The raw snapshot is saved "
            "at %s -- inspect with --dump-tables, then --reparse-all once fixed.",
            snapshot_path,
        )
        return 0

    current_range = find_current_range(html)
    directions = resolve_directions(rows, current_range, parse_directions(html))
    meetings = sorted({r["meeting_date"] for r in rows})
    log.info("parsed %d cells across %d meetings (%s); current range %s",
             len(rows), len(meetings), ", ".join(meetings[:6]), current_range or "unknown")

    if dry_run:
        log.info("[dry-run] would write %d bucket + %d direction series",
                 len(rows), len(directions) * 3)
        return 0

    stats = persist(rows, directions, scrape_date=scrape_date,
                    snapshot_path=snapshot_path, current_range=current_range)
    pruned = prune_snapshots()
    log.info("inserted %d, updated %d, unchanged %d%s",
             stats["inserted"], stats["updated"], stats["unchanged"],
             f"; pruned {pruned} old snapshots" if pruned else "")
    return stats["inserted"] + stats["updated"]


def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Scrape the CME FedWatch probability grid.")
    p.add_argument("--from-file", type=Path, help="parse an existing HTML snapshot")
    p.add_argument("--reparse-all", action="store_true",
                   help="reparse every snapshot in data/fedwatch_raw/")
    p.add_argument("--headed", action="store_true", help="run Chromium visibly")
    p.add_argument("--dry-run", action="store_true", help="parse but do not write")
    p.add_argument("--dump-tables", action="store_true",
                   help="print candidate tables and exit")
    args = p.parse_args()

    if args.reparse_all:
        raw_dir = config.resolve_path(
            config.section("fedwatch").get("raw_dir", "data/fedwatch_raw")
        )
        snapshots = sorted(raw_dir.glob("fedwatch_*.html"))
        log.info("reparsing %d snapshots", len(snapshots))
        for snap in snapshots:
            run(from_file=snap, dry_run=args.dry_run)
        return

    run(from_file=args.from_file, headed=args.headed,
        dry_run=args.dry_run, dump=args.dump_tables)


if __name__ == "__main__":
    _main()
