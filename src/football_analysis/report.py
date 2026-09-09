from __future__ import annotations

from pathlib import Path

import duckdb

from .storage import init_database


def status_report(db_path: str | Path) -> str:
    db = Path(db_path)
    if not db.exists():
        return "数据库尚未建立。请先运行 collect-once。"
    init_database(db)
    with duckdb.connect(str(db), read_only=True) as con:
        counts = con.execute("""SELECT
          (SELECT count(DISTINCT match_key) FROM source_matches
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM market_snapshots
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM match_results
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(DISTINCT source_id) FROM source_matches
             WHERE source_id IN ('source_a','source_b')),
          (SELECT max(captured_at) FROM market_snapshots
             WHERE source_id IN ('source_a','source_b')),
          (SELECT count(*) FROM objective_features)""").fetchone()
        sources = con.execute("""SELECT source_id, count(DISTINCT source_match_id), count(*), max(captured_at)
          FROM market_snapshots WHERE source_id IN ('source_a','source_b')
          GROUP BY source_id ORDER BY source_id""").fetchall()
        backfill = con.execute("""SELECT source_id, status, pages_completed,
          oldest_match_time, newest_match_time, unique_payloads, consecutive_empty, last_run_at
          FROM backfill_state WHERE source_id IN ('source_a','source_b')
          ORDER BY source_id""").fetchall()
        raw = con.execute("""SELECT count(*), coalesce(sum(byte_count),0),
          coalesce(sum(stored_bytes),0) FROM payload_objects""").fetchone()
    original, stored = int(raw[1]), int(raw[2])
    saving = (1 - stored / original) * 100 if original else 0
    lines = ["足球数据仓库状态",
             f"比赛：{counts[0]}  赔率记录：{counts[1]}  已有赛果：{counts[2]}  数据源：{counts[3]}",
             f"客观模型字段：{counts[5]}  原始对象：{raw[0]}  压缩节省：{saving:.1f}%",
             f"最近采集：{counts[4]}"]
    lines += [f"{s}: 比赛 {m}，赔率 {o}，最近 {t}" for s, m, o, t in sources]
    lines += [
        f"{source}历史：{status}，断点第{pages}页，范围 {oldest} 至 {newest}，"
        f"唯一响应{payloads}，连续无新增{empty}次，最近{last_run}"
        for source, status, pages, oldest, newest, payloads, empty, last_run in backfill
    ]
    return "\n".join(lines)


def movement_report(db_path: str | Path, limit: int = 30) -> str:
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute("""WITH series AS (
          SELECT m.source_id, n.competition, n.home_team, n.away_team, m.market, m.selection, m.line,
                 first(m.odds ORDER BY m.captured_at) opening,
                 last(m.odds ORDER BY m.captured_at) latest,
                 min(m.odds) low, max(m.odds) high, count(*) samples
          FROM market_snapshots m JOIN normalized_matches n USING(match_key)
          WHERE m.source_id IN ('source_a','source_b')
            AND m.market IN ('SPF','RQSPF') GROUP BY ALL)
          SELECT *, round((latest/opening-1)*100, 2) move_pct FROM series
          WHERE samples >= 2 ORDER BY abs(move_pct) DESC LIMIT ?""", [limit]).fetchall()
    if not rows:
        return "目前只有一次快照；至少完成两次采集后才会产生赔率变化报告。"
    lines = ["赔率变化（仅描述数据，不是下单建议）"]
    for source, league, home, away, market, selection, line, opening, latest, low, high, samples, pct in rows:
        lines.append(f"{source} | {league} {home} vs {away} | {market} {selection} line={line} | "
                     f"{opening:.2f}→{latest:.2f} ({pct:+.2f}%)，样本{samples}")
    return "\n".join(lines)


def quality_report(db_path: str | Path) -> str:
    init_database(db_path)
    with duckdb.connect(str(db_path), read_only=True) as con:
        cross = con.execute("""SELECT count(*) FROM (
          SELECT match_key FROM source_matches
          WHERE source_id IN ('source_a','source_b') GROUP BY match_key
          HAVING count(DISTINCT source_id) >= 2)""").fetchone()[0]
        compared = con.execute("""WITH latest AS (
          SELECT *, row_number() OVER (
            PARTITION BY source_id, match_key, market, selection, line
            ORDER BY captured_at DESC) rn
          FROM market_snapshots WHERE market IN ('SPF','RQSPF'))
          SELECT count(*), avg(abs(a.odds-b.odds)), max(abs(a.odds-b.odds))
          FROM latest a JOIN latest b
            ON a.match_key=b.match_key AND a.market=b.market AND a.selection=b.selection
           AND coalesce(a.line,999)=coalesce(b.line,999)
          WHERE a.source_id='source_a' AND b.source_id='source_b' AND a.rn=1 AND b.rn=1""").fetchone()
        result_days = con.execute("""SELECT count(*), min(n.kickoff_time), max(n.kickoff_time)
          FROM match_results r JOIN normalized_matches n USING(match_key)
          WHERE r.source_id IN ('source_a','source_b')""").fetchone()
    mean_diff = float(compared[1] or 0)
    max_diff = float(compared[2] or 0)
    return "\n".join([
        "数据质量",
        f"双源对齐比赛：{cross}",
        f"可比赔率项：{compared[0]}，两源平均差：{mean_diff:.4f}，最大差：{max_diff:.4f}",
        f"已沉淀赛果：{result_days[0]}，时间范围：{result_days[1]} 至 {result_days[2]}",
    ])


def full_report(db_path: str | Path, limit: int = 30) -> str:
    return "\n\n".join([status_report(db_path), quality_report(db_path), movement_report(db_path, limit)])
