from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import joblib
import numpy as np

from .adapters import adapter_for
from .storage import init_database


def latest_match_analysis(
    db_path: str | Path,
    model_dir: str | Path,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            """WITH ranked AS (
              SELECT m.source_id, m.match_key, m.selection, m.fair_probability,
                     m.captured_at,
                     row_number() OVER (
                       PARTITION BY m.source_id, m.match_key, m.selection
                       ORDER BY m.captured_at DESC) rn
              FROM market_snapshots m
              WHERE m.source_id IN ('source_a','source_b') AND m.market='SPF'),
            per_source AS (
              SELECT source_id, match_key,
                max(CASE WHEN selection='H' THEN fair_probability END) p_h,
                max(CASE WHEN selection='D' THEN fair_probability END) p_d,
                max(CASE WHEN selection='A' THEN fair_probability END) p_a,
                max(captured_at) captured_at
              FROM ranked WHERE rn=1 GROUP BY source_id, match_key),
            combined AS (
              SELECT match_key, avg(p_h) p_h, avg(p_d) p_d, avg(p_a) p_a,
                     count(*) source_count, max(captured_at) captured_at
              FROM per_source
              WHERE p_h IS NOT NULL AND p_d IS NOT NULL AND p_a IS NOT NULL
              GROUP BY match_key)
            SELECT n.match_key, n.competition, n.home_team, n.away_team,
                   n.kickoff_time, c.p_h, c.p_d, c.p_a,
                   c.source_count, c.captured_at
            FROM combined c JOIN normalized_matches n USING(match_key)
            WHERE n.kickoff_time >= current_timestamp - INTERVAL 1 DAY
            ORDER BY n.kickoff_time, n.match_key LIMIT ?""",
            [limit],
        ).fetchall()

    model = None
    model_path = Path(model_dir) / "spf_logistic.joblib"
    if model_path.exists():
        try:
            model = joblib.load(model_path)
        except Exception:
            model = None

    output: list[dict[str, Any]] = []
    for key, league, home, away, kickoff, ph, pd, pa, sources, captured in rows:
        market = {
            "H": round(float(ph), 6),
            "D": round(float(pd), 6),
            "A": round(float(pa), 6),
        }
        model_probabilities = None
        if model is not None:
            try:
                predicted = model.predict_proba(
                    np.asarray([[ph, pd, pa]], dtype=float)
                )[0]
                model_probabilities = {
                    str(label): round(float(value), 6)
                    for label, value in zip(model.classes_, predicted)
                }
            except Exception:
                model_probabilities = None
        output.append({
            "match_key": key,
            "competition": league,
            "home_team": home,
            "away_team": away,
            "kickoff_time": kickoff.isoformat() if kickoff else None,
            "source_count": int(sources),
            "data_updated_at": captured.isoformat() if captured else None,
            "market_probabilities": market,
            "model_probabilities": model_probabilities,
        })
    return output


def warehouse_snapshot(
    db_path: str | Path,
    data_root: str | Path,
    limit: int = 50,
) -> dict[str, Any]:
    db = init_database(db_path)
    with duckdb.connect(str(db), read_only=True) as con:
        counts = con.execute("""SELECT
          (SELECT count(DISTINCT match_key) FROM source_matches
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM market_snapshots
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM match_results
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM objective_features WHERE source_id='source_b'),
          (SELECT max(captured_at) FROM market_snapshots
             WHERE source_id IN ('source_a','source_b'))""").fetchone()
        health = con.execute("""SELECT source_id, status, checked_at,
          consecutive_failures, last_success_at, latency_ms, http_status,
          records_seen, error_message FROM source_health
          WHERE source_id IN ('source_a','source_b') ORDER BY source_id""").fetchall()
        backfill = con.execute("""SELECT source_id, status, pages_completed,
          oldest_match_time, newest_match_time, last_run_at, error_message
          FROM backfill_state WHERE source_id IN ('source_a','source_b')
          ORDER BY source_id""").fetchall()

    metrics_path = Path(data_root) / "models" / "metrics.json"
    if metrics_path.exists():
        try:
            model = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            model = {"status": "unavailable"}
    else:
        model = {"status": "waiting_for_samples"}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "warehouse": {
            "matches": int(counts[0]),
            "odds_records": int(counts[1]),
            "results": int(counts[2]),
            "objective_features": int(counts[3]),
            "latest_collection": counts[4].isoformat() if counts[4] else None,
        },
        "sources": [{
            "source_id": row[0],
            "name": adapter_for(row[0]).display_name,
            "status": row[1],
            "checked_at": row[2].isoformat() if row[2] else None,
            "consecutive_failures": int(row[3] or 0),
            "last_success_at": row[4].isoformat() if row[4] else None,
            "latency_ms": row[5],
            "http_status": row[6],
            "records_seen": int(row[7] or 0),
            "error": "健康检查失败，请查看 VPS 日志" if row[8] else None,
        } for row in health],
        "backfill": [{
            "source_id": row[0],
            "status": row[1],
            "pages_completed": int(row[2] or 0),
            "oldest_match_time": row[3].isoformat() if row[3] else None,
            "newest_match_time": row[4].isoformat() if row[4] else None,
            "last_run_at": row[5].isoformat() if row[5] else None,
            "error": "历史回填失败，请查看 VPS 日志" if row[6] else None,
        } for row in backfill],
        "model": model,
        "matches": latest_match_analysis(db, Path(data_root) / "models", limit),
    }


def publish_snapshot(
    db_path: str | Path,
    data_root: str | Path,
    limit: int = 50,
) -> Path:
    root = Path(data_root)
    public = root / "public"
    public.mkdir(parents=True, exist_ok=True)
    output = public / "snapshot.json"
    temporary = public / "snapshot.json.tmp"
    snapshot = warehouse_snapshot(db_path, root, limit)
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    return output
