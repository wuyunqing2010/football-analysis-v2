from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .storage import init_database


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(db_path: str | Path, output_dir: str | Path, keep: int = 14) -> dict:
    db, output = Path(db_path), Path(output_dir)
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    init_database(db)
    output.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db)) as con:
        con.execute("CHECKPOINT")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = output / f"football-{stamp}.duckdb"
    shutil.copy2(db, archive)
    digest = _sha256_file(archive)
    with duckdb.connect(str(archive), read_only=True) as con:
        con.execute("SELECT count(*) FROM normalized_matches").fetchone()
    backups = sorted(output.glob("football-*.duckdb"), reverse=True)
    for old in backups[max(1, keep):]:
        # Scope is restricted to the exact rotating-backup directory and filename pattern.
        old.unlink()
    with duckdb.connect(str(db)) as con:
        con.execute("INSERT OR REPLACE INTO backups VALUES (?, ?, ?, ?)",
                    [str(archive), datetime.now(timezone.utc), archive.stat().st_size, digest])
    return {"status": "ok", "path": str(archive), "bytes": archive.stat().st_size,
            "sha256": digest, "retained": min(len(backups), max(1, keep))}
