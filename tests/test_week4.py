"""Offline tests for forecast capture, Brier scoring, divergence and health.

No network, no secrets.  python -m pytest -q
"""
from __future__ import annotations

import pytest

from src import brier, db, divergence, forecasts, health, notion_sync, poller

NOW = "2026-09-14T18:00:00Z"
FED = "0xfed"     # Sep 16 Fed "No change" -- tracked in config
JOBS = "0xjobs"   # Oct 2 jobs "0-50k"     -- tracked in config
OTHER = "0xother" # untracked sub-market on the Fed board


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVATIONS_DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with db.get_connection() as c:
        c.executescript(notion_sync.SYNC_STATE_SQL)
        forecasts.init(c)
        brier.init(c)
        divergence.init(c)
        _seed(c)
        yield c


def _mk(c, mid, slug, name, cat, question, outcome, rd):
    db.upsert_market(c, market_id=mid, event_slug=slug, event_name=name, category=cat,
                     question=question, outcome_name=outcome, yes_token_id="y", no_token_id="n",
                     polymarket_url=None, resolution_date=rd)


def _snap(c, mid, ts, p):
    db.save_price_snapshot(c, market_id=mid, yes_price=p, no_price=1 - p, timestamp=ts)


def _seed(c):
    _mk(c, FED, "fed-decision-in-september-762", "Fed Decision — September 2026", "Fed",
        "Will there be no change in Fed interest rates after the September 2026 meeting?",
        "No change", "2026-09-16")
    _mk(c, OTHER, "fed-decision-in-september-762", "Fed Decision — September 2026", "Fed",
        "Will the Fed increase interest rates by 25 bps after the September 2026 meeting?",
        "25 bps increase", "2026-09-16")
    _mk(c, JOBS, "how-many-jobs-added-in-september-2026", "September 2026 Jobs Report", "Economic",
        "Will 0-50k jobs be added in September 2026?", "0-50k", "2026-10-02")
    # Fed "No change": drifts from 0.40 on Sep 12 to 0.05 before settling NO.
    for ts, p in [("2026-09-12T12:00:00Z", 0.40), ("2026-09-14T12:00:00Z", 0.30),
                  ("2026-09-15T12:00:00Z", 0.12), ("2026-09-16T17:00:00Z", 0.05),
                  ("2026-09-16T19:00:00Z", 0.001)]:
        _snap(c, FED, ts, p)
    for ts, p in [("2026-09-14T12:00:00Z", 0.65), ("2026-09-16T19:00:00Z", 0.999)]:
        _snap(c, OTHER, ts, p)
    for ts, p in [("2026-09-14T12:00:00Z", 0.55)]:
        _snap(c, JOBS, ts, p)


# --------------------------------------------------------------------------
# forecasts: timestamped, append-only, no hindsight
# --------------------------------------------------------------------------

def test_record_first_forecast_captures_price_at_log(conn):
    assert forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63,
                            source="cli", now=NOW) == "inserted"
    row = forecasts.first(conn, FED, "ke")
    assert row["probability"] == 0.63
    assert row["logged_at"] == NOW
    assert row["price_at_log"] == 0.30          # nearest on-or-before Sep 14 18:00
    assert row["price_at_log_ts"] == "2026-09-14T12:00:00Z"


def test_edit_becomes_revision_and_first_capture_is_preserved(conn):
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63, source="cli", now=NOW)
    assert forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.20,
                            source="cli", now="2026-09-15T18:00:00Z") == "revised"
    assert forecasts.first(conn, FED, "ke")["probability"] == 0.63
    assert forecasts.latest(conn, FED, "ke")["revision"] == 1
    assert forecasts.latest(conn, FED, "ke")["probability"] == 0.20


def test_same_value_is_unchanged_not_duplicated(conn):
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63, source="cli", now=NOW)
    assert forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63,
                            source="cli", now="2026-09-15T00:00:00Z") == "unchanged"
    n = conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    assert n == 1


def test_forecast_refused_after_resolution_date(conn):
    with pytest.raises(forecasts.ForecastRefused):
        forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63,
                         source="cli", now="2026-09-17T00:00:00Z")


def test_forecast_refused_once_tape_shows_settled(conn):
    # Resolution date is still "today" (Sep 16) but the tape has gone to 0.001.
    with pytest.raises(forecasts.ForecastRefused):
        forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63,
                         source="cli", now="2026-09-16T23:00:00Z")


def test_forecast_allowed_on_resolution_day_while_still_trading(conn):
    # Jobs market: only one mid-range tick, resolution Oct 2; Oct 2 09:00 is fine.
    assert forecasts.record(conn, market_id=JOBS, forecaster="ke", probability=0.5,
                            source="cli", now="2026-10-02T09:00:00Z") == "inserted"


def test_forecast_rejects_out_of_range_and_unknown_market(conn):
    with pytest.raises(ValueError):
        forecasts.record(conn, market_id=FED, forecaster="ke", probability=1.5, source="cli", now=NOW)
    with pytest.raises(ValueError):
        forecasts.record(conn, market_id="0xnope", forecaster="ke", probability=0.5, source="cli", now=NOW)


def test_forecasters_are_independent_rows(conn):
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63, source="cli", now=NOW)
    forecasts.record(conn, market_id=FED, forecaster="claude", probability=0.63, source="cli", now=NOW)
    assert forecasts.first(conn, FED, "ke")["revision"] == 0
    assert forecasts.first(conn, FED, "claude")["revision"] == 0


def test_capture_from_notion_records_reads_and_tolerates_percent_slip(conn, monkeypatch):
    from src import resolve
    reads = {"3d7eea342aaf810cbdd3c00d2647eec7": {"independent_read": 63,   # whole number slip
                                                   "opening_probability": None, "actual_outcome": None},
             "3d7eea342aaf81689a92f7c6d8cca35b": {"independent_read": None,
                                                   "opening_probability": None, "actual_outcome": None}}
    monkeypatch.setattr(resolve, "read_notion_row", lambda pid: reads[pid])
    monkeypatch.setattr(db, "utcnow_iso", lambda: NOW)
    stats = forecasts.capture_from_notion(conn)
    assert stats["inserted"] == 1 and stats["empty"] == 1 and stats["refused"] == 0
    assert forecasts.first(conn, FED, "keandre")["probability"] == pytest.approx(0.63)
    # second run: nothing new
    assert forecasts.capture_from_notion(conn)["unchanged"] == 1


# --------------------------------------------------------------------------
# brier: forecaster vs market at forecast time, not at close
# --------------------------------------------------------------------------

def test_brier_formula():
    assert brier.brier(0.63, 0) == pytest.approx(0.3969)
    assert brier.brier(0.63, 1) == pytest.approx(0.1369)
    assert brier.brier(None, 1) is None


def test_outcome_from_tape_requires_resolution_passed_and_extreme_price(conn):
    assert brier.outcome_from_tape(conn, FED, "2026-09-16", now="2026-09-16T23:00:00Z") is None
    assert brier.outcome_from_tape(conn, FED, "2026-09-16", now="2026-09-17T01:00:00Z") == 0
    assert brier.outcome_from_tape(conn, OTHER, "2026-09-16", now="2026-09-17T01:00:00Z") == 1
    assert brier.outcome_from_tape(conn, JOBS, "2026-10-02", now="2026-10-03T01:00:00Z") is None  # 0.55, unsettled


def test_score_uses_market_price_at_forecast_time(conn):
    forecasts.record(conn, market_id=FED, forecaster="claude", probability=0.63, source="cli", now=NOW)
    rows = brier.score_all(conn, now="2026-09-17T01:00:00Z")
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == 0
    assert r["market_p_at_forecast"] == 0.30
    assert r["brier_forecast"] == pytest.approx(0.3969)
    assert r["brier_market_at_forecast"] == pytest.approx(0.09)
    assert r["market_p_close"] == 0.05            # last live tick, terminal 0.001 skipped
    assert r["brier_market_close"] == pytest.approx(0.0025)
    # persisted and idempotent
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
    brier.score_all(conn, now="2026-09-17T02:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_score_skips_open_markets_and_revisions(conn):
    forecasts.record(conn, market_id=JOBS, forecaster="ke", probability=0.5, source="cli", now=NOW)
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.63, source="cli", now=NOW)
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.10, source="cli",
                     now="2026-09-15T18:00:00Z")   # hindsight-ish revision must not score
    rows = brier.score_all(conn, now="2026-09-17T01:00:00Z", write=False)
    assert [(r["market_id"], r["forecast_p"]) for r in rows] == [(FED, 0.63)]


def test_report_flags_small_n_and_verdict(conn):
    forecasts.record(conn, market_id=FED, forecaster="claude", probability=0.63, source="cli", now=NOW)
    text = brier.report(brier.score_all(conn, now="2026-09-17T01:00:00Z", write=False))
    assert "claude: n=1" in text
    assert "behind market@forecast" in text
    assert "n=1 < 30" in text
    assert brier.report([]).startswith("no scored forecasts")


# --------------------------------------------------------------------------
# divergence: record, never rank
# --------------------------------------------------------------------------

def test_divergence_rows_for_mapped_markets_only(conn):
    # FedWatch HOLD for the Sep 16 meeting; nothing for the jobs market.
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HOLD",
                       value=0.08, timestamp="2026-09-14T00:00:00Z")
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HIKE",
                       value=0.90, timestamp="2026-09-14T00:00:00Z")
    rows = divergence.compute(conn, now=NOW)
    by = {r["market_id"]: r for r in rows}
    assert set(by) == {FED, OTHER}                       # jobs has no comparable mapping
    assert by[FED]["comparable_series"] == "FOMC:2026-09-16:HOLD"
    assert by[FED]["market_p"] == 0.001                  # latest tape tick, whatever it is
    assert by[FED]["divergence"] == pytest.approx(0.001 - 0.08)
    assert by[OTHER]["divergence"] == pytest.approx(0.999 - 0.90)
    assert conn.execute("SELECT COUNT(*) FROM divergence").fetchone()[0] == 2
    # append-only: a second run adds two more rows, never overwrites
    divergence.compute(conn, now="2026-09-14T19:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM divergence").fetchone()[0] == 4


def test_divergence_skips_markets_without_comparable_reading(conn):
    assert divergence.compute(conn, now=NOW) == []


def test_divergence_output_is_in_tape_order_not_ranked(conn):
    """The module must not sort by |divergence| -- that is phase-3 ranking."""
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HOLD",
                       value=0.5, timestamp="2026-09-14T00:00:00Z")
    db.save_comparable(conn, source="cme_fedwatch", series_id="FOMC:2026-09-16:HIKE",
                       value=0.5, timestamp="2026-09-14T00:00:00Z")
    rows = divergence.compute(conn, now=NOW, write=False)
    ids = [r["market_id"] for r in rows]
    active = [m["market_id"] for m in db.get_active_markets(conn) if m["market_id"] in ids]
    assert ids == active


# --------------------------------------------------------------------------
# health: staleness + forecast-due
# --------------------------------------------------------------------------

def test_staleness_fresh_and_stale(conn):
    fresh = health.check_staleness(conn, now="2026-09-16T22:00:00Z")
    assert fresh["ok"] and fresh["age_hours"] == 3.0
    stale = health.check_staleness(conn, now="2026-09-17T06:00:00Z")
    assert not stale["ok"] and "STALE" in stale["message"]
    conn.execute("DELETE FROM price_snapshots")
    assert not health.check_staleness(conn, now=NOW)["ok"]


def test_forecast_due_fires_inside_window_and_clears_when_logged(conn):
    # Sep 14 18:00 -> Fed resolves end of Sep 16 = 54h away: outside 48h window.
    assert health.check_forecast_due(conn, now=NOW, forecaster="ke")["ok"]
    # Sep 15 06:00 -> 42h away: inside.
    r = health.check_forecast_due(conn, now="2026-09-15T06:00:00Z", forecaster="ke")
    assert not r["ok"]
    assert [d["market_id"] for d in r["due"]] == [FED]
    assert "FORECAST DUE" in r["message"]
    forecasts.record(conn, market_id=FED, forecaster="ke", probability=0.6, source="cli",
                     now="2026-09-15T07:00:00Z")
    assert health.check_forecast_due(conn, now="2026-09-15T08:00:00Z", forecaster="ke")["ok"]


def test_forecast_due_ignores_past_and_untracked_markets(conn):
    # Sep 17: Fed is past; jobs is 15 days out; OTHER is untracked -> nothing due.
    assert health.check_forecast_due(conn, now="2026-09-17T12:00:00Z", forecaster="ke")["ok"]
    # Oct 1 12:00: jobs (Oct 2) is inside the window and has no forecast.
    r = health.check_forecast_due(conn, now="2026-10-01T12:00:00Z", forecaster="ke")
    assert [d["market_id"] for d in r["due"]] == [JOBS]


# --------------------------------------------------------------------------
# poller: retired events actually stop being polled
# --------------------------------------------------------------------------

def test_retire_inactive_switches_off_scaffold_events(conn):
    _mk(conn, "0xjune", "fed-decision-in-june-825", "Fed Decision in June 2026", "Fed",
        "Will there be no change ... June", "No change", "2026-06-17")
    cfg = poller.load_market_config(include_inactive=True)
    assert any(not e.get("active", True) for e in cfg), "config should carry retired events"
    retired = poller.retire_inactive(conn, cfg)
    assert retired == 1
    ids = {m["market_id"] for m in db.get_active_markets(conn)}
    assert "0xjune" not in ids and FED in ids
    assert poller.retire_inactive(conn, cfg) == 0        # idempotent


def test_load_market_config_default_excludes_inactive():
    assert all(e.get("active", True) for e in poller.load_market_config())
    assert len(poller.load_market_config(include_inactive=True)) > len(poller.load_market_config())


# --------------------------------------------------------------------------
# notion_sync: never overwrite hand-entered once-only fields
# --------------------------------------------------------------------------

def _prop_num(v): return {"type": "number", "number": v}
def _prop_sel(v): return {"type": "select", "select": ({"name": v} if v else None)}
def _prop_date(v): return {"type": "date", "date": ({"start": v} if v else None)}


def test_has_value_semantics():
    assert notion_sync._has_value(_prop_num(0.655))
    assert not notion_sync._has_value(_prop_num(None))
    assert notion_sync._has_value(_prop_sel("YES"))
    assert not notion_sync._has_value(_prop_sel("Unresolved"))   # placeholder, not a value
    assert not notion_sync._has_value(_prop_sel(None))
    assert notion_sync._has_value(_prop_date("2026-09-09"))
    assert not notion_sync._has_value(None)


def test_protect_existing_keeps_hand_entered_values():
    props = {
        "Market": {"title": []}, "Opening Probability": {"number": 0.12},
        "Open Date": {"date": {"start": "2026-09-17"}}, "Closing Probability": {"number": None},
        "Actual Outcome": {"select": {"name": "Unresolved"}},
        "Mid-Period Probability": {"number": 0.11}, "Comparable Probability": {"number": 0.9},
    }
    existing = {"Opening Probability": True, "Open Date": True,
                "Closing Probability": True, "Actual Outcome": True, "Mid-Period Probability": True}
    out = notion_sync.protect_existing(props, existing)
    assert "Opening Probability" not in out and "Open Date" not in out
    assert "Closing Probability" not in out and "Actual Outcome" not in out
    assert "Mid-Period Probability" in out            # refreshable, not protected
    assert "Comparable Probability" in out and "Market" in out


def test_protect_existing_fills_blanks():
    props = {"Opening Probability": {"number": 0.12}, "Actual Outcome": {"select": {"name": "Unresolved"}}}
    out = notion_sync.protect_existing(props, {"Opening Probability": False, "Actual Outcome": False})
    assert set(out) == {"Opening Probability", "Actual Outcome"}


def test_run_reads_page_before_update_and_never_clobbers(conn, monkeypatch):
    """End-to-end through run(): the Fed row has hand-entered opening prob + outcome."""
    written: dict[str, dict] = {}
    pages = {"3d7eea342aaf810cbdd3c00d2647eec7": {"properties": {
        "Opening Probability": _prop_num(0.655), "Actual Outcome": _prop_sel("NO"),
        "Open Date": _prop_date("2026-09-09"), "Closing Probability": _prop_num(0.05),
        "Mid-Period Probability": _prop_num(None)}}}

    def fake_call(method, path, payload=None, **kw):
        if method == "GET" and path.startswith("/pages/"):
            return pages.get(path.split("/")[-1], {"properties": {}})
        if method == "PATCH" and path.startswith("/pages/"):
            written[path.split("/")[-1]] = payload["properties"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(notion_sync, "_call", fake_call)
    monkeypatch.setattr(notion_sync, "_database_id", lambda: "db")
    monkeypatch.setattr(notion_sync, "verify_schema", lambda: True)
    monkeypatch.setenv("NOTION_API_KEY", "x")
    conn.commit()   # run() opens its own connection; make the seed visible to it
    # Fed + jobs are the two tracked markets in this fixture; the other five rules miss.
    stats = notion_sync.run()
    assert stats["updated"] == 2 and stats["failed"] == 0
    # jobs page had no existing values -> everything written, nothing protected
    assert "Opening Probability" in written["3d7eea342aaf81689a92f7c6d8cca35b"]
    props = written["3d7eea342aaf810cbdd3c00d2647eec7"]
    for k in ("Opening Probability", "Actual Outcome", "Open Date", "Closing Probability"):
        assert k not in props, f"{k} would have been overwritten"
    assert "Mid-Period Probability" in props
    assert "Market" in props
