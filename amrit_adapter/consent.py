from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import audit, new_id, now_iso

PURPOSES = {"marketing", "service", "voice_callback", "cow_enquiry", "export_waitlist"}
STATUSES = {"granted", "withdrawn", "stop_all"}


def record_consent(conn: Connection, brand_id: str, contact_id: str, channel_type: str, purpose: str,
                   status: str, evidence: str | None, effective_at: str | None = None) -> str:
    if purpose not in PURPOSES or status not in STATUSES:
        raise ValueError("invalid purpose/status")
    cid = new_id()
    conn.execute(
        text("""INSERT INTO consent_ledger(consent_id, brand_id, contact_id, channel_type, purpose, status, evidence,
                recorded_at, effective_at) VALUES (:i,:b,:c,:ch,:p,:s,:e,:r,:ef)"""),
        {"i": cid, "b": brand_id, "c": contact_id, "ch": channel_type, "p": purpose, "s": status, "e": evidence,
         "r": now_iso(), "ef": effective_at or now_iso()},
    )
    audit(conn, "adapter", "consent.recorded", contact_id, {"purpose": purpose, "status": status, "channel": channel_type})
    return cid


def has_consent(conn: Connection, brand_id: str, contact_id: str, channel_type: str, purpose: str) -> bool:
    """Latest effective row for (contact, channel, purpose). A 'stop_all' on any purpose/channel wins.
    Cross-brand consent is never implied: brand_id is part of the lookup."""
    stop = conn.execute(
        text("""SELECT 1 FROM consent_ledger WHERE brand_id=:b AND contact_id=:c AND status='stop_all' LIMIT 1"""),
        {"b": brand_id, "c": contact_id},
    ).fetchone()
    if stop:
        return False
    row = conn.execute(
        text("""SELECT status FROM consent_ledger WHERE brand_id=:b AND contact_id=:c AND channel_type=:ch AND purpose=:p
                ORDER BY effective_at DESC, recorded_at DESC LIMIT 1"""),
        {"b": brand_id, "c": contact_id, "ch": channel_type, "p": purpose},
    ).fetchone()
    return bool(row and row[0] == "granted")
