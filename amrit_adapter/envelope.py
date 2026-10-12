from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import new_id, now_iso

SCHEMA_VERSION = "1.0"

AuthLevel = Literal["verified_signature", "static_token_hint", "unauthenticated"]


class InternalEvent(BaseModel):
    """Internal event envelope required by the kit (Stage 2)."""

    brand_id: str
    source: str                      # shopify | razorpay | shiprocket | bitespeed | manual
    source_event_id: str             # provider's unique delivery/event id
    source_object_id: str | None = None
    event_type: str                  # normalized, e.g. shopify.checkouts.update
    occurred_at: str
    received_at: str = Field(default_factory=now_iso)
    schema_version: str = SCHEMA_VERSION
    correlation_id: str = Field(default_factory=new_id)
    auth_level: AuthLevel = "unauthenticated"
    payload: dict[str, Any] = Field(default_factory=dict)


class InboxResult(BaseModel):
    event_id: str
    duplicate: bool


def persist_event(conn: Connection, ev: InternalEvent) -> InboxResult:
    """Durably store the event. Duplicate (source, source_event_id) is reported, not re-inserted."""
    existing = conn.execute(
        text("SELECT event_id FROM event_inbox WHERE source=:s AND source_event_id=:e"),
        {"s": ev.source, "e": ev.source_event_id},
    ).fetchone()
    if existing:
        return InboxResult(event_id=existing[0], duplicate=True)
    event_id = new_id()
    conn.execute(
        text(
            """INSERT INTO event_inbox(event_id, brand_id, source, source_event_id, source_object_id, event_type,
               occurred_at, received_at, schema_version, correlation_id, auth_level, payload_json)
               VALUES (:id,:b,:s,:e,:o,:t,:oa,:ra,:sv,:c,:al,:p)"""
        ),
        {
            "id": event_id, "b": ev.brand_id, "s": ev.source, "e": ev.source_event_id, "o": ev.source_object_id,
            "t": ev.event_type, "oa": ev.occurred_at, "ra": ev.received_at, "sv": ev.schema_version,
            "c": ev.correlation_id, "al": ev.auth_level, "p": json.dumps(ev.payload, default=str),
        },
    )
    return InboxResult(event_id=event_id, duplicate=False)
