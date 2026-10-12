"""Shipment state machine.

States are distinct on purpose: label/AWB created is NOT picked up; pickup scheduled is NOT picked up.
Out-of-order events: an older event never replaces a later-ranked state. Terminal states (delivered, rto_delivered)
are never overwritten by transit states.

Shiprocket status-id mapping is UNVERIFIED for most states (public docs only show ids 5, 6, 18, 20, 42 in a
sample payload, fetched 2026-10-11). The mapping below therefore uses the status *label* first and the id
only as a secondary hint. Unknown labels become 'unknown' and open an internal exception rather than a
customer-facing message.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import audit, new_id, now_iso

STATE_RANK = {
    "label_created": 1,       # AWB assigned / manifest generated
    "pickup_scheduled": 2,
    "picked_up": 3,
    "in_transit": 4,
    "out_for_delivery": 5,
    "delivery_exception": 5,  # NDR: same rank as OFD; can alternate
    "delivered": 9,
    "rto_initiated": 6,
    "rto_in_transit": 7,
    "rto_delivered": 9,
    "cancelled": 9,
    "lost": 9,
    "unknown": 0,
}
TERMINAL = {"delivered", "rto_delivered", "cancelled", "lost"}
CUSTOMER_VISIBLE = {"picked_up", "in_transit", "out_for_delivery", "delivery_exception", "delivered", "rto_initiated"}

LABEL_MAP = {
    "manifest generated": "label_created", "awb assigned": "label_created", "new": "label_created",
    "pickup scheduled": "pickup_scheduled", "pickup generated": "pickup_scheduled", "pickup queued": "pickup_scheduled",
    "picked up": "picked_up", "shipped": "picked_up",
    "in transit": "in_transit", "reached destination hub": "in_transit", "misrouted": "in_transit",
    "out for delivery": "out_for_delivery",
    "undelivered": "delivery_exception", "ndr": "delivery_exception", "delivery exception": "delivery_exception",
    "delivered": "delivered",
    "rto initiated": "rto_initiated", "rto in transit": "rto_in_transit", "rto delivered": "rto_delivered",
    "cancelled": "cancelled", "canceled": "cancelled", "lost": "lost", "damaged": "lost",
}
# Secondary hint only (ids seen in Shiprocket's public sample payload; everything else UNVERIFIED)
ID_HINT = {"5": "label_created", "42": "picked_up", "6": "picked_up", "18": "in_transit", "20": "in_transit"}


def normalize(provider_status: str | None, provider_status_id: str | None) -> str:
    s = (provider_status or "").strip().lower()
    if s in LABEL_MAP:
        return LABEL_MAP[s]
    for k, v in LABEL_MAP.items():
        if k in s:
            return v
    return ID_HINT.get(str(provider_status_id or ""), "unknown")


@dataclass(frozen=True)
class ShipmentUpdate:
    shipment_key: str
    previous_state: str
    new_state: str
    applied: bool
    customer_notify: bool
    reason: str


def ensure_shipment(conn: Connection, shipment_key: str, order_key: str, awb: str | None, courier: str | None) -> None:
    row = conn.execute(text("SELECT 1 FROM shipment_ref WHERE shipment_key=:k"), {"k": shipment_key}).fetchone()
    if not row:
        conn.execute(text("""INSERT INTO shipment_ref(shipment_key, order_key, awb, courier, state, state_at, updated_at)
                             VALUES (:k,:o,:a,:c,'unknown',NULL,:u)"""),
                     {"k": shipment_key, "o": order_key, "a": awb, "c": courier, "u": now_iso()})


def apply_event(conn: Connection, shipment_key: str, provider_status: str, provider_status_id: str | None,
                occurred_at: str, auth_level: str) -> ShipmentUpdate:
    new_state = normalize(provider_status, provider_status_id)
    event_key = f"{shipment_key}:{provider_status_id or provider_status}:{occurred_at}"
    cur = conn.execute(text("SELECT state, state_at FROM shipment_ref WHERE shipment_key=:k"), {"k": shipment_key}).fetchone()
    if cur is None:
        raise ValueError("unknown shipment")
    dup = conn.execute(text("SELECT applied FROM shipment_event WHERE event_key=:k"), {"k": event_key}).fetchone()
    if dup:
        return ShipmentUpdate(shipment_key, cur.state, cur.state, False, False, "duplicate event")

    prev = cur.state
    applied = True
    reason = "applied"
    if new_state == "unknown":
        applied, reason = False, "unmapped provider status -> internal exception"
        _exception(conn, shipment_key, f"unmapped status '{provider_status}' id={provider_status_id}")
    elif prev in TERMINAL and new_state != prev:
        applied, reason = False, "terminal state preserved"
    elif cur.state_at and occurred_at and occurred_at < cur.state_at and STATE_RANK[new_state] <= STATE_RANK.get(prev, 0):
        applied, reason = False, "older event ignored (out-of-order)"
    elif STATE_RANK[new_state] < STATE_RANK.get(prev, 0) and new_state not in ("delivery_exception", "rto_initiated"):
        applied, reason = False, "lower-ranked state ignored"

    conn.execute(
        text("""INSERT INTO shipment_event(event_key, shipment_key, provider_status, provider_status_id, normalized_state,
                occurred_at, received_at, applied) VALUES (:k,:s,:ps,:pid,:n,:oa,:ra,:ap)"""),
        {"k": event_key, "s": shipment_key, "ps": provider_status, "pid": provider_status_id, "n": new_state,
         "oa": occurred_at, "ra": now_iso(), "ap": int(applied)},
    )
    if applied:
        conn.execute(text("UPDATE shipment_ref SET state=:s, state_at=:t, exception_open=:e, updated_at=:u WHERE shipment_key=:k"),
                     {"s": new_state, "t": occurred_at, "e": int(new_state == "delivery_exception"), "u": now_iso(), "k": shipment_key})
        if new_state == "delivery_exception":
            _exception(conn, shipment_key, "NDR received: human task unless verified reattempt action exists")
    audit(conn, "adapter", "shipment.event", shipment_key, {"from": prev, "to": new_state, "applied": applied, "auth": auth_level})
    # Customer-visible notification only for applied, customer-visible, and (if static token) API-reverified states.
    notify = applied and new_state in CUSTOMER_VISIBLE and auth_level == "verified_api"
    return ShipmentUpdate(shipment_key, prev, new_state if applied else prev, applied, notify, reason)


def _exception(conn: Connection, shipment_key: str, reason: str) -> None:
    conn.execute(
        text("""INSERT INTO support_case(case_id, brand_id, contact_id, order_key, case_type, owner_team, priority, status, created_at, updated_at)
                SELECT :i, o.brand_id, o.contact_id, o.order_key, 'ndr', 'dispatch', 'high', 'open', :n, :n
                FROM shipment_ref s JOIN order_ref o ON o.order_key=s.order_key WHERE s.shipment_key=:k"""),
        {"i": new_id(), "n": now_iso(), "k": shipment_key},
    )
    audit(conn, "adapter", "shipment.exception", shipment_key, {"reason": reason})
