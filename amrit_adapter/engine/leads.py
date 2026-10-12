"""Lead creation/update with canonical stage + structured tags (brief §2)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import audit, new_id, now_iso

STAGES = ["enquiry", "product_selected", "address_confirmed", "quoted", "payment_pending", "paid", "ordered", "fulfilled", "lost"]


def ensure_contact(conn: Connection, brand_id: str, phone_e164: str, display_name: str | None = None) -> str:
    row = conn.execute(text("SELECT contact_id FROM contact_channel WHERE brand_id=:b AND channel_type='whatsapp' AND address=:a"),
                       {"b": brand_id, "a": phone_e164}).fetchone()
    if row:
        return row[0]
    cid = new_id()
    conn.execute(text("INSERT INTO contact(contact_id, brand_id, display_name, created_at) VALUES (:c,:b,:n,:t)"),
                 {"c": cid, "b": brand_id, "n": display_name, "t": now_iso()})
    conn.execute(text("INSERT INTO contact_channel(channel_id, contact_id, brand_id, channel_type, address, verified) VALUES (:i,:c,:b,'whatsapp',:a,1)"),
                 {"i": new_id(), "c": cid, "b": brand_id, "a": phone_e164})
    return cid


def upsert_lead(conn: Connection, brand_id: str, contact_id: str, product_key: str, stage: str, source: str = "whatsapp") -> tuple[str, bool]:
    """One open lead per (contact, product). Returns (lead_id, created). Stage only moves forward unless 'lost'."""
    assert stage in STAGES
    row = conn.execute(text("SELECT lead_id, stage FROM lead WHERE brand_id=:b AND contact_id=:c AND lead_type=:t AND stage<>'lost'"),
                       {"b": brand_id, "c": contact_id, "t": f"product:{product_key}"}).fetchone()
    now = now_iso()
    if row:
        if STAGES.index(stage) > STAGES.index(row.stage):
            conn.execute(text("UPDATE lead SET stage=:s, updated_at=:u WHERE lead_id=:l"), {"s": stage, "u": now, "l": row.lead_id})
        lead_id, created = row.lead_id, False
    else:
        lead_id, created = new_id(), True
        conn.execute(text("""INSERT INTO lead(lead_id, brand_id, contact_id, lead_type, stage, external_ref, created_at, updated_at)
                             VALUES (:l,:b,:c,:t,:s,:x,:n,:n)"""),
                     {"l": lead_id, "b": brand_id, "c": contact_id, "t": f"product:{product_key}", "s": stage, "x": source, "n": now})
    prior = conn.execute(text("SELECT COUNT(*) FROM lead WHERE brand_id=:b AND contact_id=:c AND lead_id<>:l"), {"b": brand_id, "c": contact_id, "l": lead_id}).scalar()
    tags = {"LEAD_RETURNING" if prior else "LEAD_NEW", f"INTEREST_{product_key.upper()}", f"CHANNEL_{source.upper()}", f"STAGE_{stage.upper()}"}
    for t in tags:
        conn.execute(text("INSERT OR IGNORE INTO lead_tag(lead_id, tag, added_at) VALUES (:l,:t,:n)"), {"l": lead_id, "t": t, "n": now})
    # STAGE_* tags: keep only the current one
    conn.execute(text("DELETE FROM lead_tag WHERE lead_id=:l AND tag LIKE 'STAGE_%' AND tag<>:cur"), {"l": lead_id, "cur": f"STAGE_{stage.upper()}"})
    audit(conn, "engine", "lead.upsert", lead_id, {"product": product_key, "stage": stage, "created": created})
    return lead_id, created


def tags_for(conn: Connection, lead_id: str) -> set[str]:
    return {r[0] for r in conn.execute(text("SELECT tag FROM lead_tag WHERE lead_id=:l"), {"l": lead_id})}
