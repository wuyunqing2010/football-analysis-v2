from datetime import datetime, timedelta, timezone

import duckdb

from football_analysis.collect import (
    _insert,
    parse_source_a,
    parse_source_b,
)
from football_analysis.storage import init_database


def test_source_a_mapping():
    payload = {"data": {"2026-09-09": {"1": {
        "match_id": "a1", "serial_no": "3001", "league_name": "韩职",
        "host_name_s": "主队", "guest_name_s": "客队", "match_time": "2026-09-09 18:30:00",
        "sale_status": "1", "bet_time": "2026-09-09 18:25:00",
        "list": {"SportteryNWDL": {"boundary": "0", "odds": {"3": "2.92", "1": "2.90", "0": "2.26"}}}
    }}}}
    rows = parse_source_a(payload)
    assert len(rows) == 1
    assert rows[0].markets == (("SPF", "H", None, 2.92), ("SPF", "D", None, 2.9), ("SPF", "A", None, 2.26))


def test_source_b_result_and_markets():
    item = {
        "concede": 1, "spfSpArr": ["2.92", "2.90", "2.26"],
        "rqspfSpArr": ["1.48", "3.85", "5.15"], "zjqsSpArr": ["9","4","3","3","4","6","10","15"],
        "bfSpArr": [], "bqcSpArr": [], "matchInfo": {"homeTeamName": "主", "awayTeamName": "客",
        "leagueName": "联赛", "matchTime": "2026-09-08 18:30:00"},
        "race": {"id": 1, "raceNo": "260908001", "sellStopTime": "2026-09-08 18:30:00",
        "status": {"name": "已开固定奖"}, "raceZc": {"finalScore": "1:0", "halfScore": "0:0", "matchResult": []}}
    }
    rows = parse_source_b({"data": {"dayRaceVoList": [{"raceVoList": [item]}]}})
    assert rows[0].final_score == "1:0"
    assert len(rows[0].markets) == 14


def test_source_a_nested_history_result():
    payload = {"data": {"pages": [{"records": [{
        "matchId": "old-1", "raceNo": "周一001", "leagueName": "联赛",
        "homeTeamName": "主队", "awayTeamName": "客队",
        "matchTime": "2026-08-01 18:30:00",
        "result": {"homeScore": 2, "awayScore": 1},
    }]}]}}
    rows = parse_source_a(payload)
    assert len(rows) == 1
    assert rows[0].final_score == "2:1"


def test_identical_odds_are_not_written_twice(tmp_path):
    db = tmp_path / "x.duckdb"
    init_database(db)
    payload = {"data": {"2026-09-09": {"1": {
        "match_id": "a1", "serial_no": "3001", "league_name": "韩职",
        "host_name_s": "主队", "guest_name_s": "客队",
        "match_time": "2026-09-09 18:30:00",
        "list": {"SportteryNWDL": {"odds": {
            "3": "2.92", "1": "2.90", "0": "2.26"
        }}},
    }}}}
    rows = parse_source_a(payload)
    first = _insert(db, "r1", datetime.now(timezone.utc), "a" * 64, rows)
    second = _insert(
        db, "r2", datetime.now(timezone.utc) + timedelta(minutes=5),
        "a" * 64, rows
    )
    assert first["odds"] == 3
    assert second["odds"] == 0
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM market_snapshots").fetchone()[0] == 3

