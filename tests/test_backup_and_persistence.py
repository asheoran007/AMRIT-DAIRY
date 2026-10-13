"""Backup + restart persistence: a file DB survives engine re-creation; backups are consistent copies, rotated, and
visible on /health."""
import sqlite3
import time

from sqlalchemy import text

from amrit_adapter import backup
from amrit_adapter.db import make_engine, migrate


def test_file_db_persists_across_engine_restart(tmp_path):
    db = tmp_path / "adapter.db"
    url = f"sqlite:///{db}"
    eng = make_engine(url); migrate(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO shopify_token(shop_domain, access_token, scope, obtained_at) VALUES ('s.myshopify.com','tok','read_orders','2026-10-13T00:00:00Z')"))
    eng.dispose()
    eng2 = make_engine(url); migrate(eng2)                       # "restart": new process, same disk, migrations idempotent
    with eng2.begin() as c:
        assert c.execute(text("SELECT access_token FROM shopify_token")).scalar() == "tok"
        assert c.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar() >= 5


def test_backup_is_consistent_copy_rotated_and_reported(tmp_path):
    db = tmp_path / "adapter.db"
    url = f"sqlite:///{db}"
    eng = make_engine(url); migrate(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO shopify_token(shop_domain, access_token, scope, obtained_at) VALUES ('s.myshopify.com','tok','read_orders','2026-10-13T00:00:00Z')"))
    p = backup.maybe_backup(url, keep=3, force=True)
    assert p is not None and p.parent.name == "backups"
    copy = sqlite3.connect(str(p))
    assert copy.execute("SELECT access_token FROM shopify_token").fetchone()[0] == "tok"
    copy.close()
    assert backup.status["last_backup"] == p.name and backup.status["error"] is None and backup.status["files"] == 1
    # rotation: only `keep` newest files survive
    for i in range(5):
        (p.parent / f"adapter-2026010{i}-0000.db").write_bytes(b"old")
    backup.run_backup(db, keep=3)
    assert len(list(p.parent.glob("adapter-*.db"))) == 3
    # interval gate: a call inside the interval is a no-op; non-file urls are ignored safely
    backup._next = time.time() + 1000
    assert backup.maybe_backup(url) is None
    assert backup.maybe_backup("sqlite://", force=True) is None


def test_health_exposes_backup_status():
    import amrit_adapter.api as api
    from fastapi.testclient import TestClient
    b = TestClient(api.app).get("/health").json()["backup"]
    assert set(b) == {"last_backup", "files", "error"}
