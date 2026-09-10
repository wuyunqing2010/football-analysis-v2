from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx

from .adapters import configured_adapters
from .collect import HEADERS
from .config import load_sites
from .storage import init_database


def _health_status(success: bool, consecutive_failures: int, records_seen: int = 1) -> str:
    if success:
        return "ok" if records_seen > 0 else "warning"
    return "down" if consecutive_failures >= 3 else "warning"


def check_sources(
    config_path: str | Path,
    data_root: str | Path,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    db = Path(db_path) if db_path else root / "football.duckdb"
    init_database(db)
    checked_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {"checked_at": checked_at.isoformat(), "sources": {}}

    for site, adapter in configured_adapters(load_sites(config_path)):
        endpoint = site.url
        started = time.monotonic()
        http_status: int | None = None
        records_seen = 0
        error: str | None = None
        try:
            endpoint = adapter.live_endpoint(site)
            with httpx.Client(
                headers={**HEADERS, "Referer": site.url},
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
            ) as client:
                response = client.get(endpoint)
                http_status = response.status_code
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("接口未返回 JSON 对象")
            records_seen = len(adapter.parser(payload))
            success = True
        except Exception as exc:
            success = False
            error = f"{type(exc).__name__}: {exc}"[:500]

        latency_ms = round((time.monotonic() - started) * 1000, 1)
        with duckdb.connect(str(db)) as con:
            previous = con.execute(
                "SELECT consecutive_failures, last_success_at FROM source_health WHERE source_id=?",
                [site.id],
            ).fetchone()
            failures = 0 if success else int((previous or [0])[0] or 0) + 1
            last_success = checked_at if success else (previous[1] if previous else None)
            status = _health_status(success, failures, records_seen)
            con.execute(
                """INSERT INTO source_health
                   (source_id, checked_at, status, consecutive_failures,
                    last_success_at, latency_ms, http_status, records_seen,
                    error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     checked_at=excluded.checked_at,
                     status=excluded.status,
                     consecutive_failures=excluded.consecutive_failures,
                     last_success_at=excluded.last_success_at,
                     latency_ms=excluded.latency_ms,
                     http_status=excluded.http_status,
                     records_seen=excluded.records_seen,
                     error_message=excluded.error_message""",
                [
                    site.id,
                    checked_at,
                    status,
                    failures,
                    last_success,
                    latency_ms,
                    http_status,
                    records_seen,
                    error,
                ],
            )
        summary["sources"][site.id] = {
            "name": adapter.display_name,
            "status": status,
            "consecutive_failures": failures,
            "http_status": http_status,
            "records_seen": records_seen,
            "latency_ms": latency_ms,
            "error": error,
        }

    temporary = root / "last-health.json.tmp"
    output = root / "last-health.json"
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return summary
