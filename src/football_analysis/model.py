from __future__ import annotations

import json
from pathlib import Path

import duckdb
import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss


def train_and_backtest(db_path: str | Path, output_dir: str | Path, minimum: int = 60) -> dict:
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute("""WITH results AS (
          SELECT match_key, first(final_score ORDER BY captured_at DESC) final_score
          FROM match_results WHERE source_id IN ('source_a','source_b')
          GROUP BY match_key),
        ranked AS (
          SELECT m.match_key, m.selection, m.fair_probability, m.odds, m.captured_at,
                 row_number() OVER (PARTITION BY m.match_key, m.selection ORDER BY m.captured_at DESC) rn
          FROM market_snapshots m JOIN normalized_matches n USING(match_key)
          JOIN results r USING(match_key)
          WHERE m.source_id IN ('source_a','source_b')
            AND m.market='SPF' AND m.captured_at < n.kickoff_time),
        wide AS (
          SELECT match_key,
            max(CASE WHEN selection='H' THEN fair_probability END) p_h,
            max(CASE WHEN selection='D' THEN fair_probability END) p_d,
            max(CASE WHEN selection='A' THEN fair_probability END) p_a
          FROM ranked WHERE rn=1 GROUP BY match_key)
        SELECT w.p_h,w.p_d,w.p_a,r.final_score,n.kickoff_time
        FROM wide w JOIN results r USING(match_key)
        JOIN normalized_matches n USING(match_key)
        WHERE p_h IS NOT NULL AND p_d IS NOT NULL AND p_a IS NOT NULL
        ORDER BY n.kickoff_time""").fetchall()
    if len(rows) < minimum:
        return {"status": "insufficient_data", "usable_matches": len(rows), "required": minimum,
                "message": "继续回填历史数据后自动重试，不生成虚假模型。"}
    x, y = [], []
    for ph, pd, pa, score, _ in rows:
        try:
            home, away = map(int, score.split(":"))
        except (ValueError, AttributeError):
            continue
        x.append([float(ph), float(pd), float(pa)])
        y.append("H" if home > away else "D" if home == away else "A")
    if len(x) < minimum:
        return {"status": "insufficient_data", "usable_matches": len(x), "required": minimum,
                "message": "继续回填历史数据后自动重试，不生成虚假模型。"}
    split = max(1, int(len(x) * 0.8))
    if len(set(y[:split])) < 3 or len(x) - split < 10:
        return {"status": "insufficient_classes", "usable_matches": len(x),
                "message": "历史样本类别不足，暂不训练。"}
    model = LogisticRegression(max_iter=2000, class_weight="balanced")
    model.fit(np.asarray(x[:split]), np.asarray(y[:split]))
    probabilities = model.predict_proba(np.asarray(x[split:]))
    predictions = model.predict(np.asarray(x[split:]))
    metrics = {
        "status": "ok", "train_matches": split, "test_matches": len(x) - split,
        "accuracy": round(float(accuracy_score(y[split:], predictions)), 6),
        "log_loss": round(float(log_loss(y[split:], probabilities, labels=list(model.classes_))), 6),
        "classes": list(model.classes_),
        "warning": "历史回测仅衡量模型，不代表未来结果或收益。",
    }
    output = Path(output_dir); output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output / "spf_logistic.joblib")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics
