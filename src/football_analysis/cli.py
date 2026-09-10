from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_sites
from .collect import collect_once
from .backup import create_backup
from .history import backfill
from .health import check_sources
from .maintenance import optimize_storage
from .model import train_and_backtest
from .probe import run_probe
from .report import full_report, status_report
from .storage import init_database
from .summary import summarize
from .snapshot import latest_match_analysis, publish_snapshot
from .web import serve


DEFAULT_DATA_ROOT = Path("/var/lib/football-data")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="football-analysis")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-db", help="create the DuckDB schema")
    init.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))

    probe = subparsers.add_parser("probe", help="run a read-only browser probe")
    probe.add_argument("--config", default="config/sites.local.json")
    probe.add_argument("--output", default=str(DEFAULT_DATA_ROOT / "probes"))

    summary = subparsers.add_parser("summarize", help="summarize captured JSON endpoints")
    summary.add_argument("--input", default=str(DEFAULT_DATA_ROOT / "probes"))
    summary.add_argument("--output", default=str(DEFAULT_DATA_ROOT / "probe-summary.json"))

    collect = subparsers.add_parser("collect-once", help="collect and normalize both odds sources")
    collect.add_argument("--config", default="config/sites.local.json")
    collect.add_argument("--data", default=str(DEFAULT_DATA_ROOT))
    collect.add_argument("--db", default=None)

    status = subparsers.add_parser("status", help="show warehouse status")
    status.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))

    report = subparsers.add_parser("report", help="show odds movement report")
    report.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))
    report.add_argument("--limit", type=int, default=30)

    history = subparsers.add_parser("backfill", help="collect historical data from configured sites")
    history.add_argument("--config", default="config/sites.local.json")
    history.add_argument("--data", default=str(DEFAULT_DATA_ROOT))
    history.add_argument("--db", default=None)
    history.add_argument("--max-actions", type=int, default=50)
    history.add_argument("--max-replay", type=int, default=500)

    train = subparsers.add_parser("train", help="train and chronologically backtest after enough history exists")
    train.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))
    train.add_argument("--output", default=str(DEFAULT_DATA_ROOT / "models"))
    train.add_argument("--minimum", type=int, default=60)

    backup = subparsers.add_parser("backup", help="create a verified rotating database backup")
    backup.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))
    backup.add_argument("--output", default=str(DEFAULT_DATA_ROOT / "backups"))
    backup.add_argument("--keep", type=int, default=14)

    optimize = subparsers.add_parser("optimize", help="checkpoint database and clean partial compressed files")
    optimize.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))
    optimize.add_argument("--data", default=str(DEFAULT_DATA_ROOT))

    health = subparsers.add_parser("health-check", help="check and record source health")
    health.add_argument("--config", default="config/sites.local.json")
    health.add_argument("--data", default=str(DEFAULT_DATA_ROOT))
    health.add_argument("--db", default=None)

    publish = subparsers.add_parser("publish", help="publish a sanitized read-only snapshot")
    publish.add_argument("--data", default=str(DEFAULT_DATA_ROOT))
    publish.add_argument("--db", default=None)
    publish.add_argument("--limit", type=int, default=100)

    analyze = subparsers.add_parser("analyze", help="print latest public match probabilities")
    analyze.add_argument("--db", default=str(DEFAULT_DATA_ROOT / "football.duckdb"))
    analyze.add_argument("--models", default=str(DEFAULT_DATA_ROOT / "models"))
    analyze.add_argument("--limit", type=int, default=50)

    web = subparsers.add_parser("serve", help="serve the mobile status page and read-only API")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--data", default=str(DEFAULT_DATA_ROOT))
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "init-db":
        print(f"Database ready: {init_database(args.db)}")
    elif args.command == "probe":
        reports = run_probe(load_sites(args.config), args.output)
        for report in reports:
            print(
                f"{report['source_id']}: status={report['status']} "
                f"json={report['json_response_count']} title={report['title']!r}"
            )
    elif args.command == "summarize":
        result = summarize(args.input)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"Summary saved: {output}")
    elif args.command == "collect-once":
        print(json.dumps(collect_once(args.config, args.data, args.db), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(status_report(args.db))
    elif args.command == "report":
        print(full_report(args.db, args.limit))
    elif args.command == "backfill":
        print(json.dumps(backfill(
            args.config, args.data, args.db, args.max_actions, args.max_replay
        ), ensure_ascii=False, indent=2))
    elif args.command == "train":
        print(json.dumps(train_and_backtest(args.db, args.output, args.minimum), ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(json.dumps(create_backup(args.db, args.output, args.keep), ensure_ascii=False, indent=2))
    elif args.command == "optimize":
        print(json.dumps(optimize_storage(args.db, args.data), ensure_ascii=False, indent=2))
    elif args.command == "health-check":
        print(json.dumps(check_sources(args.config, args.data, args.db), ensure_ascii=False, indent=2))
    elif args.command == "publish":
        db = args.db or str(Path(args.data) / "football.duckdb")
        print(f"Published: {publish_snapshot(db, args.data, args.limit)}")
    elif args.command == "analyze":
        print(json.dumps(latest_match_analysis(args.db, args.models, args.limit), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        serve(args.host, args.port, args.data)


if __name__ == "__main__":
    main()
