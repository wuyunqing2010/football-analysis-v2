from __future__ import annotations

from pathlib import Path

import duckdb

from .storage import init_database


def optimize_storage(db_path: str | Path, data_root: str | Path) -> dict:
    db = init_database(db_path)
    root = Path(data_root)
    before = db.stat().st_size
    with duckdb.connect(str(db)) as con:
        con.execute("CHECKPOINT")
        con.execute("VACUUM")
        rows = con.execute(
            """SELECT count(*), coalesce(sum(byte_count), 0),
                      coalesce(sum(stored_bytes), 0)
               FROM payload_objects"""
        ).fetchone()
    after = db.stat().st_size
    # Only abandoned atomic-write fragments are removable. Historical payloads
    # and normalized records are never deleted by maintenance.
    partials = 0
    objects = root / "raw" / "objects"
    if objects.exists():
        for path in objects.glob("*/*.tmp"):
            if path.is_file():
                path.unlink()
                partials += 1
    original = int(rows[1])
    stored = int(rows[2])
    savings = round((1 - stored / original) * 100, 2) if original else 0.0
    return {
        "status": "ok",
        "database_bytes_before": before,
        "database_bytes_after": after,
        "raw_objects": int(rows[0]),
        "raw_uncompressed_bytes": original,
        "raw_stored_bytes": stored,
        "raw_savings_percent": savings,
        "partial_files_removed": partials,
    }
