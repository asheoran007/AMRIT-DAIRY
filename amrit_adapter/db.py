from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def migrate(engine: Engine) -> list[str]:
    """Apply SQL migrations in order. Returns the versions applied in this call."""
    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        ))
        done = {r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            for stmt in _split_statements(sql):
                conn.execute(text(stmt))
            # record the version even if the migration file forgot its own INSERT (safety net)
            conn.execute(text("INSERT INTO schema_migrations(version, applied_at) SELECT :v, CURRENT_TIMESTAMP "
                              "WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version=:v)"), {"v": version})
            applied.append(version)
    return applied


def _split_statements(sql: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


@contextmanager
def tx(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn


def audit(conn: Connection, actor: str, action: str, subject: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        text("INSERT INTO audit_log(audit_id, at, actor, action, subject, detail_json) VALUES (:i,:t,:a,:ac,:s,:d)"),
        {"i": new_id(), "t": now_iso(), "a": actor, "ac": action, "s": subject, "d": json.dumps(detail or {}, default=str)},
    )
