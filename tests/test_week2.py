"""Offline tests for the week-2 modules. No network, no secrets.

    python -m pytest -q

The FedWatch fixtures are not invented: `fedwatch_current_view_2026-08-05.html`
mirrors the layout of the live tool as inspected in a browser on 2026-08-05,
including the details that broke the first parser -- the "(Current)" suffix on
the standing range, and NOW / 1 DAY / 1 WEEK / 1 MONTH as columns.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import config, db, fedwatch, fred, notion_sync

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CURRENT_VIEW = FIXTURES / "fedwatch_current_view_2026-08-05.html"
WIDE_VIEW = FIXTURES / "fedwatch_2026-08-04_sample.html"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVATIONS_DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db.get_connection() as c:
        c.executescript(notion_sync.SYNC_STATE_SQL)
        yield c


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def test_config_loads_and_has_the_sections_modules_expect():
    assert config.section("fred")["series"] == ["DFF", "DFEDTARU", "DFEDTARL"]
    assert "cmegroup.com" in config.section("fedwatch")["page_url"]
    assert config.section("notion")["database_id"]


def test_notion_system_and_user_properties_do_not_overlap():
    cfg = config.section("notion")
    assert not set(cfg["system_properties"]) & set(cfg["user_properties"])


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------

def test_fred_drops_missing_values():
    """FRED uses '.' for holidays. Writing those as 0.0 would corrupt averages."""
    points = fred.to_points("DFF", [
        {"date": "2026-08-01", "value": "4.33"},
        {"date": "2026-08-02", "value": "."},
        {"date": "2026-08-03", "value": ""},
        {"date": "2026-08-04", "value": "4.33"},
    ])
    assert [p["timestamp"] for p in points] == [
        "2026-08-01T00:00:00Z", "2026-08-04T00:00:00Z"
    ]
    assert points[0]["value"] == 4.33


def test_fred_save_is_idempotent_across_overlapping_windows(conn):
    """The trailing window re-requests the same days every run. The tape has no
    unique constraint, so this must not accumulate duplicates."""
    point = fred.to_points("DFF", [{"date": "2026-08-04", "value": "4.33"}])[0]
    assert fred.save_point(conn, point) == "inserted"
    assert fred.save_point(conn, point) == "unchanged"
    assert fred.save_point(conn, point) == "unchanged"
    count = conn.execute("SELECT COUNT(*) FROM comparables").fetchone()[0]
    assert count == 1


def test_fred_revision_updates_in_place(conn):
    fred.save_point(conn, fred.to_points("DFF", [{"date": "2026-08-04", "value": "4.33"}])[0])
    result = fred.save_point(
        conn, fred.to_points("DFF", [{"date": "2026-08-04", "value": "4.31"}])[0]
    )
    assert result == "updated"
    rows = conn.execute("SELECT value, notes FROM comparables").fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == 4.31
    assert rows[0]["notes"] == "fred revision"


def test_target_midpoint_is_derived(conn):
    for series, value in (("DFEDTARU", "3.75"), ("DFEDTARL", "3.50")):
        fred.save_point(conn, fred.to_points(series, [{"date": "2026-08-04", "value": value}])[0])
    assert fred.derive_target_midpoint(conn) == 1
    mid = conn.execute(
        "SELECT value FROM comparables WHERE series_id = 'DFEDTARMID'"
    ).fetchone()[0]
    assert mid == pytest.approx(3.625)


# --------------------------------------------------------------------------
# FedWatch normalisers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("375-400", "375-400"),
    ("3.75-4.00", "375-400"),
    ("375 – 400", "375-400"),
    # The live tool marks the standing range like this. Dropping it would lose
    # the single most important row on the page.
    ("350-375 (Current)", "350-375"),
    ("350–375 (Current)", "350-375"),
    ("MEETING DATE", None),
    ("62.4%", None),
    ("400-375", None),
])
def test_normalise_range(raw, expected):
    assert fedwatch.normalise_range(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("62.4%", 0.624), ("0.0%", 0.0), ("100%", 1.0), ("abc", None), ("450%", None),
])
def test_normalise_percent(raw, expected):
    assert fedwatch.normalise_percent(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("16 Sep 2026", "2026-09-16"),
    ("2026-09-16", "2026-09-16"),
    ("09/16/2026", "2026-09-16"),
    ("September 16, 2026", "2026-09-16"),
    ("MEETING DATE", None),
])
def test_normalise_meeting_date(raw, expected):
    assert fedwatch.normalise_meeting_date(raw) == expected


# --------------------------------------------------------------------------
# FedWatch parsing -- the live "Current" view
# --------------------------------------------------------------------------

def test_parses_the_live_current_view_layout():
    rows = fedwatch.parse_probability_grid(CURRENT_VIEW.read_text())
    assert len(rows) == 6  # 2 meeting tabs x 3 buckets

    sep = {r["target_range"]: r["probability"] for r in rows
           if r["meeting_date"] == "2026-09-16"}
    # Must take the NOW column (45.1%), not 1 DAY (41.6%).
    assert sep["350-375"] == pytest.approx(0.451)
    assert sep["375-400"] == pytest.approx(0.549)
    assert sum(sep.values()) == pytest.approx(1.0, abs=0.001)


def test_parses_the_wide_grid_layout_too():
    rows = fedwatch.parse_probability_grid(WIDE_VIEW.read_text())
    assert len(rows) == 18
    assert sorted({r["meeting_date"] for r in rows}) == [
        "2026-09-16", "2026-10-28", "2026-12-09"
    ]


def test_parse_returns_empty_on_junk_html():
    assert fedwatch.parse_probability_grid("<html><body><p>nope</p></body></html>") == []


def test_current_range_read_from_the_page_itself():
    # "Current target rate is 350–375" uses an en dash on the live site.
    assert fedwatch.find_current_range(CURRENT_VIEW.read_text()) == "350-375"


def test_published_ease_nochange_hike_is_parsed():
    published = fedwatch.parse_directions(CURRENT_VIEW.read_text())
    assert published["2026-09-16"]["HOLD"] == pytest.approx(0.451)
    assert published["2026-09-16"]["HIKE"] == pytest.approx(0.549)
    assert published["2026-09-16"]["CUT"] == pytest.approx(0.0)


def test_our_collapse_agrees_with_the_tools_own_numbers():
    """The real cross-check: collapsing the bucket grid ourselves must
    reproduce the EASE / NO CHANGE / HIKE the tool publishes."""
    html = CURRENT_VIEW.read_text()
    rows = fedwatch.parse_probability_grid(html)
    ours = fedwatch.collapse_directions(rows, fedwatch.find_current_range(html))
    for meeting, published in fedwatch.parse_directions(html).items():
        for direction, value in published.items():
            assert ours[meeting][direction] == pytest.approx(value, abs=0.001)


def test_published_wins_and_disagreement_is_recorded():
    html = CURRENT_VIEW.read_text()
    rows = fedwatch.parse_probability_grid(html)
    bogus = {"2026-09-16": {"CUT": 0.0, "HOLD": 0.10, "HIKE": 0.90}}
    out = fedwatch.resolve_directions(rows, "350-375", bogus)
    assert out["2026-09-16"]["values"]["HOLD"] == pytest.approx(0.10)
    assert out["2026-09-16"]["basis"] == "published"
    assert out["2026-09-16"]["disagreement"]["HOLD"] == pytest.approx(0.351, abs=0.001)


def test_falls_back_to_derived_when_nothing_published():
    rows = fedwatch.parse_probability_grid(CURRENT_VIEW.read_text())
    out = fedwatch.resolve_directions(rows, "350-375", {})
    assert out["2026-09-16"]["basis"] == "derived"
    assert out["2026-09-16"]["disagreement"] is None


def test_fedwatch_persist_is_idempotent(conn, monkeypatch, tmp_path):
    html = CURRENT_VIEW.read_text()
    rows = fedwatch.parse_probability_grid(html)
    directions = fedwatch.resolve_directions(
        rows, fedwatch.find_current_range(html), fedwatch.parse_directions(html)
    )
    first = fedwatch.persist(rows, directions, scrape_date="2026-08-05",
                             snapshot_path=None, current_range="350-375")
    second = fedwatch.persist(rows, directions, scrape_date="2026-08-05",
                              snapshot_path=None, current_range="350-375")
    assert first["inserted"] == 12   # 6 buckets + 2 meetings x 3 directions
    assert second["inserted"] == 0
    assert second["unchanged"] == 12


# --------------------------------------------------------------------------
# Comparable matching
# --------------------------------------------------------------------------

@pytest.mark.parametrize("outcome,expected", [
    ("No change", "HOLD"),
    ("25 bps decrease", "CUT"),
    ("50+ bps decrease", "CUT"),
    ("25 bps increase", "HIKE"),
    ("Something else entirely", None),
])
def test_outcome_maps_to_direction(outcome, expected):
    assert notion_sync.direction_for(outcome, "") == expected


def test_comparable_series_built_from_outcome_and_resolution_date():
    """No manual link table: the sub-market already carries what we need."""
    assert notion_sync.comparable_series_for({
        "category": "Fed", "resolution_date": "2026-09-16",
        "outcome_name": "No change", "question": "Will the Fed hold?",
    }) == ("cme_fedwatch", "FOMC:2026-09-16:HOLD")


def test_non_fed_markets_get_no_comparable():
    assert notion_sync.comparable_series_for({
        "category": "Election", "resolution_date": "2026-11-03",
        "outcome_name": "Democrat", "question": "Who wins?",
    }) is None


def test_fomc_dates_come_from_events_yml():
    dates = notion_sync.fomc_dates()
    assert "2026-09-16" in dates and "2026-12-09" in dates
    assert dates == sorted(dates)


@pytest.mark.parametrize("outcome,expected", [
    ("3.75%", "350-375"),
    ("4.0%", "375-400"),
    ("1.25", "100-125"),
    # Open-ended buckets map to no single FedWatch range.
    ("≥ 4.5%", None),
    ("≤1.0%", None),
    ("No change", None),
])
def test_rate_level_maps_to_a_fedwatch_bucket(outcome, expected):
    assert notion_sync.level_bucket_for(outcome) == expected


def test_rate_level_market_uses_the_last_fomc_before_resolution():
    """"Rate at end of 2026" resolves 12-31, but the last meeting is 12-09."""
    assert notion_sync.comparable_series_for({
        "category": "Fed", "resolution_date": "2026-12-31",
        "outcome_name": "3.75%",
        "question": "Will the upper bound of the target federal funds rate be 3.75%?",
    }) == ("cme_fedwatch", "FOMC:2026-12-09:350-375")


def test_cuts_per_year_market_gets_no_comparable():
    """The real trap in this repo. The question contains the word "cut", but it
    resolves on 2026-12-31 -- not an FOMC date -- and FedWatch publishes no
    cuts-per-year series. A confident wrong comparable is worse than none."""
    assert notion_sync.comparable_series_for({
        "category": "Fed", "resolution_date": "2026-12-31",
        "outcome_name": "3 (75 bps)",
        "question": "Will 3 Fed rate cuts happen in 2026?",
    }) is None


def test_meeting_outcome_markets_map_to_directions():
    """The five sub-markets of a Fed decision event, as they actually appear."""
    def series(outcome, question):
        return notion_sync.comparable_series_for({
            "category": "Fed", "resolution_date": "2026-09-16",
            "outcome_name": outcome, "question": question,
        })

    assert series("No change", "Will there be no change in Fed interest rates?") \
        == ("cme_fedwatch", "FOMC:2026-09-16:HOLD")
    assert series("25 bps decrease", "Will the Fed decrease rates by 25 bps?") \
        == ("cme_fedwatch", "FOMC:2026-09-16:CUT")
    assert series("50+ bps decrease", "Will the Fed decrease rates by 50+ bps?") \
        == ("cme_fedwatch", "FOMC:2026-09-16:CUT")
    assert series("25 bps increase", "Will the Fed increase rates by 25 bps?") \
        == ("cme_fedwatch", "FOMC:2026-09-16:HIKE")
    assert series("50+ bps increase", "Will the Fed increase rates by 50+ bps?") \
        == ("cme_fedwatch", "FOMC:2026-09-16:HIKE")


# --------------------------------------------------------------------------
# Rollup
# --------------------------------------------------------------------------

def _seed_market(conn, market_id="0xabc", outcome="No change"):
    db.upsert_market(
        conn, market_id=market_id, event_slug="fed-decision-sep",
        event_name="Fed Decision September 2026", category="Fed",
        question=f"Fed decision September 2026 - {outcome}?",
        outcome_name=outcome, yes_token_id="tok1", no_token_id="tok2",
        polymarket_url="https://polymarket.com/event/fed-decision-sep",
        resolution_date="2026-09-16",
    )
    return market_id


def test_rollup_picks_open_mid_close(conn):
    market_id = _seed_market(conn)
    for ts, price in [
        ("2026-08-01T00:00:00Z", 0.40),
        ("2026-08-16T00:00:00Z", 0.55),   # exact midpoint of 08-01 .. 08-31
        ("2026-08-25T00:00:00Z", 0.61),
        ("2026-08-31T00:00:00Z", 0.70),
    ]:
        db.save_price_snapshot(conn, market_id=market_id, yes_price=price, timestamp=ts)

    row = notion_sync.build_rollup(conn)[0]
    assert row["opening_probability"] == pytest.approx(0.40)
    assert row["mid_probability"] == pytest.approx(0.55)
    assert row["closing_probability"] == pytest.approx(0.70)
    assert row["open_date"] == "2026-08-01"
    assert row["snapshot_count"] == 4


def test_rollup_handles_a_market_with_no_snapshots_yet(conn):
    _seed_market(conn)
    row = notion_sync.build_rollup(conn)[0]
    assert row["opening_probability"] is None
    assert row["snapshot_count"] == 0
    # Must still produce a valid Notion payload rather than blowing up.
    props = notion_sync.build_properties(row)
    assert props["Opening Probability"]["number"] is None


def test_rollup_attaches_the_fedwatch_comparable(conn):
    market_id = _seed_market(conn)
    db.save_price_snapshot(conn, market_id=market_id, yes_price=0.44,
                           timestamp="2026-08-10T00:00:00Z")
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HOLD",
                       value=0.451, timestamp="2026-08-05T00:00:00Z")
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HOLD",
                       value=0.512, timestamp="2026-08-20T00:00:00Z")

    row = notion_sync.build_rollup(conn)[0]
    # Open date is 08-10, so the 08-05 reading wins over the later one.
    assert row["comparable_series"] == "FOMC:2026-09-16:HOLD"
    assert row["comparable_value"] == pytest.approx(0.451)


def test_comparable_falls_back_to_earliest_reading_after_open(conn):
    """Markets predate the day FedWatch scraping starts."""
    market_id = _seed_market(conn)
    db.save_price_snapshot(conn, market_id=market_id, yes_price=0.44,
                           timestamp="2026-07-01T00:00:00Z")
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HOLD",
                       value=0.451, timestamp="2026-08-05T00:00:00Z")
    row = notion_sync.build_rollup(conn)[0]
    assert row["comparable_value"] == pytest.approx(0.451)


# --------------------------------------------------------------------------
# Notion payloads
# --------------------------------------------------------------------------

def _rollup_row(**overrides):
    base = {
        "market_id": "0xabc", "event_name": "Fed Decision September 2026",
        "question": "Fed decision September 2026 - No change?",
        "outcome_name": "No change", "category": "Fed",
        "polymarket_url": "https://polymarket.com/event/fed-decision-sep",
        "open_date": "2026-08-01", "resolution_date": "2026-09-16",
        "opening_probability": 0.40, "mid_probability": 0.55,
        "closing_probability": 0.70,
        "comparable_source": "cme_fedwatch", "comparable_value": 0.451,
    }
    base.update(overrides)
    return base


def test_payload_never_touches_user_columns():
    props = notion_sync.build_properties(_rollup_row())
    cfg = config.section("notion")
    assert not set(props) & set(cfg["user_properties"])
    assert set(props) <= set(cfg["system_properties"])


def test_payload_shape():
    props = notion_sync.build_properties(_rollup_row())
    assert props["Market"]["title"][0]["text"]["content"].startswith("Fed decision")
    assert props["Opening Probability"]["number"] == pytest.approx(0.40)
    assert props["Comparable Probability"]["number"] == pytest.approx(0.451)
    assert props["Comparable Source"]["select"]["name"] == "FedWatch"
    assert props["Open Date"]["date"]["start"] == "2026-08-01"


def test_a_fred_rate_is_not_written_as_a_probability():
    """3.88 in a percent-formatted probability field would render 388%."""
    props = notion_sync.build_properties(
        _rollup_row(comparable_source="fred", comparable_value=3.88)
    )
    assert props["Comparable Probability"]["number"] is None
    assert props["Comparable Source"]["select"]["name"] == "FRED"


def test_unknown_category_is_omitted_rather_than_rejected_by_notion():
    props = notion_sync.build_properties(_rollup_row(category="Sports"))
    assert "Category" not in props


def test_no_comparable_means_source_none():
    props = notion_sync.build_properties(
        _rollup_row(comparable_source=None, comparable_value=None)
    )
    assert props["Comparable Source"]["select"]["name"] == "None"
    assert props["Comparable Probability"]["number"] is None


def test_payload_hash_is_stable_and_change_sensitive():
    a = notion_sync.payload_hash(notion_sync.build_properties(_rollup_row()))
    b = notion_sync.payload_hash(notion_sync.build_properties(_rollup_row()))
    c = notion_sync.payload_hash(
        notion_sync.build_properties(_rollup_row(opening_probability=0.41))
    )
    assert a == b and a != c
