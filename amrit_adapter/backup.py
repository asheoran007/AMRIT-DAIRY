"""Daily SQLite backup on the persistent disk (+ Render's own disk snapshots as the second line).

The worker calls maybe_backup() every pass; once per BACKUP_INTERVAL_S (default 24 h) it writes a consistent copy with
sqlite3's online backup API to <db dir>/backups/adapter-YYYYMMDD-HHMM.db and keeps the newest BACKUP_KEEP files.
Restore = stop the service, copy a backup over adapter.db, start (BACKUP-AND-ROLLBACK.md). /health shows the last one.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("amrit.backup")
status: dict = {"last_backup": None, "files": 0, "error": None}
_next = 0.0


def db_path_from_url(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    p = url[len("sqlite:///"):]
    return Path(p if p.startswith("/") else "./" + p)


def run_backup(db_file: Path, keep: int = 7) -> Path:
    out_dir = db_file.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"adapter-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.db"
    src = sqlite3.connect(str(db_file))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)          # online, consistent snapshot even while the app writes
        finally:
            dst.close()
    finally:
        src.close()
    files = sorted(out_dir.glob("adapter-*.db"))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
    status.update(last_backup=dest.name, files=min(len(files), keep), error=None)
    return dest


def maybe_backup(db_url: str, interval_s: int = 86400, keep: int = 7, force: bool = False) -> Path | None:
    global _next
    if not force and time.time() < _next:
        return None
    _next = time.time() + interval_s
    db_file = db_path_from_url(db_url)
    if not db_file or not db_file.exists():
        return None
    try:
        dest = run_backup(db_file, keep)
        log.info("backup written %s (%d kept)", dest.name, status["files"])
        return dest
    except Exception as exc:
        status.update(error=f"{type(exc).__name__}: {exc}"[:200])
        log.warning("backup failed: %s", type(exc).__name__)
        return None
