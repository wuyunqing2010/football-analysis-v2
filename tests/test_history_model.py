import json
import gzip
from datetime import datetime, timedelta, timezone

import duckdb

from football_analysis.collect import _record_raw_payload, _save_raw
from football_analysis.history import (
    _api_cursor,
    _load_state,
    _paged_url,
    _update_state,
    ingest_model_details,
)
from football_analysis.model import train_and_backtest
from football_analysis.storage import init_database


def test_model_details_drop_recommendations(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    payload = {"data": {"2026-09-08": {"周二001": {"modelDetailsResMap": {
        "INDEX_DIFF": {"indexDto": {
            "matchId": 7, "raceNo": "周二001", "matchTime": "2026-09-08 18:30:00",
            "compName": "韩职", "home": {"home": "主队"}, "away": {"home": "客队"},
            "sp": {"homeWin": 2.1, "draw": 3.2, "awayWin": 3.1},
            "primaryRec": "H", "indexDiffYc": {"recent": 3, "secondaryRec": "A"}
        }},
        "SPECIALIST": {"specialistRecommendVo": {"memberNum": 999}}
    }}}}}
    count = ingest_model_details(payload, db, "r1", datetime.now(timezone.utc), "a" * 64)
    assert count == 1
    assert ingest_model_details(
        payload, db, "r2", datetime.now(timezone.utc), "a" * 64
    ) == 0
    with duckdb.connect(str(db)) as con:
        model_type, feature_json = con.execute(
            "SELECT model_type, feature_json FROM objective_features"
        ).fetchone()
    assert model_type == "INDEX_DIFF"
    lowered = feature_json.lower()
    assert "primaryrec" not in lowered and "secondaryrec" not in lowered and "specialist" not in lowered


def test_training_waits_for_enough_history(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    result = train_and_backtest(db, tmp_path / "models", minimum=60)
    assert result["status"] == "insufficient_data"


def test_raw_payload_is_globally_compressed_and_deduplicated(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    payload = {"data": {"odds": [2.1, 3.2, 3.4]}}
    first, digest = _save_raw(
        tmp_path, "source_a", datetime.now(timezone.utc), payload
    )
    second, digest2 = _save_raw(
        tmp_path, "source_a", datetime.now(timezone.utc) + timedelta(days=2), payload
    )
    assert first == second
    assert digest == digest2
    assert gzip.open(first, "rt", encoding="utf-8").read().startswith("{")
    assert _record_raw_payload(
        db, "run-1", "source_a", datetime.now(timezone.utc),
        "https://example.invalid/api/match/history", first, digest
    )
    assert not _record_raw_payload(
        db, "run-2", "source_a", datetime.now(timezone.utc),
        "https://example.invalid/api/match/history", second, digest2
    )


def test_api_page_cursor_and_backfill_state(tmp_path):
    records = [{
        "url": "https://example.invalid/api/match/history?pageIndex=7&pageSize=20",
        "method": "GET",
        "request_json": None,
    }]
    cursor = _api_cursor(records)
    assert cursor["page_value"] == 7
    assert "pageIndex=8" in _paged_url(cursor["url"], cursor["page_key"], 8)

    db = tmp_path / "x.duckdb"
    init_database(db)
    _update_state(
        db, "source_a", status="running", pages_completed=8,
        cursor=cursor, unique_payloads=3, matches_seen=40,
        consecutive_empty=0,
    )
    loaded = _load_state(db, "source_a")
    assert loaded["pages_completed"] == 8
    assert loaded["last_cursor"]["page_value"] == 7


def test_legacy_cursor_migrates_without_rewriting_data(tmp_path):
    db = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("""CREATE TABLE backfill_state (
          source_id VARCHAR PRIMARY KEY, oldest_match_time TIMESTAMP,
          newest_match_time TIMESTAMP, pages_completed BIGINT,
          cursor VARCHAR, last_run_at TIMESTAMPTZ, status VARCHAR,
          error_message VARCHAR)""")
        con.execute(
            "INSERT INTO backfill_state VALUES ('source_a', NULL, NULL, 12, ?, NULL, 'running', NULL)",
            [json.dumps({"strategy": "replay_then_advance", "replay_actions": 12})],
        )
    init_database(db)
    loaded = _load_state(db, "source_a")
    assert loaded["pages_completed"] == 12
    assert loaded["last_cursor"]["replay_actions"] == 12


def test_raw_payload_is_content_addressed_and_compressed(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    payload = {"data": {"odds": [2.1, 3.2, 3.4]}}
    now = datetime.now(timezone.utc)
    first, digest = _save_raw(tmp_path, "source_a", now, payload)
    second, second_digest = _save_raw(
        tmp_path, "source_a", now + timedelta(days=2), payload
    )
    assert first == second
    assert digest == second_digest
    assert json.loads(gzip.decompress(first.read_bytes())) == payload
    assert _record_raw_payload(db, "r1", "source_a", now, "https://example/a", first, digest)
    assert not _record_raw_payload(
        db, "r2", "source_a", now + timedelta(days=2),
        "https://example/a", second, digest
    )
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM payload_objects").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM raw_payloads").fetchone()[0] == 1


def test_backfill_state_is_replaced_not_added_twice(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    _update_state(
        db, "source_a", status="running", pages_completed=25,
        cursor={"replay_actions": 25}, unique_payloads=4,
        matches_seen=20, consecutive_empty=0
    )
    _update_state(
        db, "source_a", status="caught_up", pages_completed=30,
        cursor={"replay_actions": 30}, unique_payloads=5,
        matches_seen=23, consecutive_empty=1
    )
    state = _load_state(db, "source_a")
    assert state["pages_completed"] == 30
    assert state["unique_payloads"] == 5
    assert state["matches_seen"] == 23
