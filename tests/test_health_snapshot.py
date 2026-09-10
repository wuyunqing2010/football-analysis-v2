import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import duckdb

from football_analysis.adapters import active_source_ids, adapter_for
from football_analysis.health import _health_status
from football_analysis.snapshot import publish_snapshot, warehouse_snapshot
from football_analysis.storage import init_database
from football_analysis.web import PAGE, StatusHandler


def test_bundled_adapters_are_the_selected_two_sources():
    assert active_source_ids() == ("source_a", "source_b")
    assert adapter_for("source_a").display_name == "59itou"
    assert adapter_for("source_b").display_name == "tiantianyouliao"


def test_health_marks_three_consecutive_failures_down():
    assert _health_status(True, 0, 8) == "ok"
    assert _health_status(True, 0, 0) == "warning"
    assert _health_status(False, 1) == "warning"
    assert _health_status(False, 3) == "down"


def test_snapshot_is_sanitized_and_publishable_on_empty_database(tmp_path):
    db = init_database(tmp_path / "football.duckdb")
    snapshot = warehouse_snapshot(db, tmp_path, limit=10)
    assert snapshot["read_only"] is True
    assert snapshot["warehouse"]["matches"] == 0
    assert snapshot["matches"] == []
    output = publish_snapshot(db, tmp_path, limit=10)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["read_only"] is True
    assert "raw_payloads" not in loaded


def test_public_snapshot_does_not_expose_internal_error_urls(tmp_path):
    db = init_database(tmp_path / "football.duckdb")
    now = datetime.now(timezone.utc)
    with duckdb.connect(str(db)) as con:
        con.execute(
            """INSERT INTO source_health VALUES
               ('source_a', ?, 'warning', 1, NULL, 10, 403, 0, ?)""",
            [now, "https://private.invalid/path?token=secret"],
        )
        con.execute(
            """INSERT INTO backfill_state
               (source_id, pages_completed, last_run_at, status, error_message)
               VALUES ('source_a', 7, ?, 'error', ?)""",
            [now, "/root/private/path"],
        )
    encoded = json.dumps(warehouse_snapshot(db, tmp_path), ensure_ascii=False)
    assert "private.invalid" not in encoded
    assert "/root/private" not in encoded
    assert "查看 VPS 日志" in encoded


def test_mobile_page_uses_read_only_api():
    assert "/api/v1/snapshot" in PAGE
    assert "足球数据仓库" in PAGE


def test_read_only_http_api_serves_published_snapshot(tmp_path):
    db = init_database(tmp_path / "football.duckdb")
    snapshot_path = publish_snapshot(db, tmp_path, limit=10)
    StatusHandler.snapshot_path = snapshot_path
    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/api/v1/status", timeout=3
        ) as response:
            payload = json.loads(response.read())
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert payload["read_only"] is True
            assert "matches" not in payload
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
