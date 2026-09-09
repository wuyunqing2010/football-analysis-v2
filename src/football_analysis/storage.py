from __future__ import annotations

from pathlib import Path

import duckdb


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id VARCHAR PRIMARY KEY,
    source_id VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    page_title VARCHAR,
    final_url VARCHAR,
    json_response_count INTEGER DEFAULT 0,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    payload_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    source_id VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    request_method VARCHAR,
    response_status INTEGER,
    response_url VARCHAR NOT NULL,
    content_type VARCHAR,
    payload_path VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS payload_objects (
    payload_sha256 VARCHAR PRIMARY KEY,
    payload_path VARCHAR NOT NULL,
    byte_count BIGINT NOT NULL,
    stored_bytes BIGINT NOT NULL,
    compression VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_key VARCHAR PRIMARY KEY,
    competition VARCHAR,
    home_team VARCHAR,
    away_team VARCHAR,
    kickoff_time TIMESTAMPTZ,
    status VARCHAR,
    home_score INTEGER,
    away_score INTEGER,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS source_match_map (
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    match_key VARCHAR NOT NULL,
    confidence DOUBLE,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_id, source_match_id)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    market VARCHAR NOT NULL,
    selection VARCHAR NOT NULL,
    line VARCHAR,
    odds DOUBLE NOT NULL,
    raw_payload_id VARCHAR,
    PRIMARY KEY (source_id, source_match_id, captured_at, market, selection, line)
);

CREATE TABLE IF NOT EXISTS normalized_matches (
    match_key VARCHAR PRIMARY KEY,
    competition VARCHAR,
    home_team VARCHAR NOT NULL,
    away_team VARCHAR NOT NULL,
    kickoff_time TIMESTAMP,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS source_matches (
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    match_key VARCHAR NOT NULL,
    race_no VARCHAR,
    sale_status VARCHAR,
    sale_deadline TIMESTAMP,
    captured_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_id, source_match_id)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    match_key VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    market VARCHAR NOT NULL,
    selection VARCHAR NOT NULL,
    line DOUBLE,
    odds DOUBLE NOT NULL,
    implied_probability DOUBLE,
    fair_probability DOUBLE,
    payload_sha256 VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS market_match_idx
ON market_snapshots(match_key, market, captured_at);

CREATE TABLE IF NOT EXISTS match_results (
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    match_key VARCHAR NOT NULL,
    final_score VARCHAR NOT NULL,
    half_score VARCHAR,
    result_json VARCHAR,
    captured_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_id, source_match_id)
);

CREATE TABLE IF NOT EXISTS objective_features (
    feature_id VARCHAR PRIMARY KEY,
    source_id VARCHAR NOT NULL,
    source_match_id VARCHAR NOT NULL,
    match_key VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    model_type VARCHAR NOT NULL,
    feature_json VARCHAR NOT NULL,
    payload_sha256 VARCHAR NOT NULL
);

CREATE INDEX IF NOT EXISTS feature_match_idx
ON objective_features(match_key, model_type, captured_at);

CREATE TABLE IF NOT EXISTS backfill_state (
    source_id VARCHAR PRIMARY KEY,
    oldest_match_time TIMESTAMP,
    newest_match_time TIMESTAMP,
    pages_completed BIGINT DEFAULT 0,
    last_cursor VARCHAR,
    last_run_at TIMESTAMPTZ,
    status VARCHAR,
    error_message VARCHAR,
    unique_payloads BIGINT DEFAULT 0,
    matches_seen BIGINT DEFAULT 0,
    consecutive_empty INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backups (
    backup_path VARCHAR PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_metadata (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
"""


MIGRATIONS = (
    "ALTER TABLE backfill_state ADD COLUMN IF NOT EXISTS last_cursor VARCHAR",
    "ALTER TABLE backfill_state ADD COLUMN IF NOT EXISTS unique_payloads BIGINT DEFAULT 0",
    "ALTER TABLE backfill_state ADD COLUMN IF NOT EXISTS matches_seen BIGINT DEFAULT 0",
    "ALTER TABLE backfill_state ADD COLUMN IF NOT EXISTS consecutive_empty INTEGER DEFAULT 0",
)


def init_database(path: str | Path) -> Path:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as connection:
        connection.execute(SCHEMA)
        for statement in MIGRATIONS:
            connection.execute(statement)
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info('backfill_state')"
            ).fetchall()
        }
        # Early development builds used `cursor`; preserve it without touching
        # any already-normalized match, odds, result, or feature rows.
        if "cursor" in columns:
            connection.execute(
                "UPDATE backfill_state SET last_cursor=coalesce(last_cursor, cursor)"
            )
        connection.execute(
            """INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '2')
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=now()"""
        )
    return db_path
