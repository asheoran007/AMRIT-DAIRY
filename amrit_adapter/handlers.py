"""Map verified provider events to state changes and queued actions.

Event ownership (one owner per customer-facing event; see TARGET-WORKFLOWS.md):
- Shopify checkouts/update  -> adapter decides abandoned-checkout eligibility (F15). Shopify itself also sends
  abandoned-checkout emails if enabled; one must be switched off before activation (duplicate map).
- Shopify orders/paid       -> cancel abandonment jobs; order confirmation owner = Shopify email (existing) until
  a WhatsApp confirmation is approved to replace it (F10).
- Razorpay payment_link.*   -> payment reconciliation (F08/F09/F16).
- Shiprocket tracking       -> shipment state; customer notification only after authenticated API re-verification (F11/F18).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import outbox, payments, shipments
from .db import audit, new_id, now_iso
from .envelope import InternalEvent


def _minor(amount_str: str | float | int | None) -> int:
    if amount_str is None:
        return 0
    return int(round(float(amount_str) * 100))


def upsert_order_from_shopify(conn: Connection, brand_id: str, body: dict, source_kind: str) -> str:
    """source_kind: 'checkout' or 'order'. Creates/updates order_ref. Contact linkage via identity_map only."""
    sid = str(body.get("id"))
    order_key = f"{brand_id}:shopify:{source_kind}:{sid}"
    currency = body.get("currency") or body.get("presentment_currency") or "INR"
    total = _minor(body.get("total_price"))
    fin = "unpaid"
    if source_kind == "order":
        fs = (body.get("financial_status") or "pending").lower()
        fin = {"paid": "paid", "partially_paid": "partially_paid", "refunded": "refunded",
               "partially_refunded": "partially_refunded", "pending": "unpaid", "authorized": "unpaid", "voided": "unpaid"}.get(fs, "unpaid")
        gateways = [g.lower() for g in (body.get("payment_gateway_names") or [])]
        payment_mode = "cod" if any("cash on delivery" in g or g == "cod" for g in gateways) else "prepaid"
        if payment_mode == "cod" and fin == "unpaid":
            fin = "cod_pending"
    else:
        payment_mode = None
    contact_id = None
    cust = body.get("customer") or {}
    if cust.get("id"):
        row = conn.execute(text("SELECT contact_id FROM identity_map WHERE brand_id=:b AND source='shopify' AND source_id=:s"),
                           {"b": brand_id, "s": str(cust["id"])}).fetchone()
        if row:
            contact_id = row[0]
        else:
            contact_id = new_id()
            conn.execute(text("INSERT INTO contact(contact_id, brand_id, display_name, created_at) VALUES (:c,:b,:n,:t)"),
                         {"c": contact_id, "b": brand_id, "n": None, "t": now_iso()})
            conn.execute(text("INSERT INTO identity_map(brand_id, source, source_id, contact_id) VALUES (:b,'shopify',:s,:c)"),
                         {"b": brand_id, "s": str(cust["id"]), "c": contact_id})
    exists = conn.execute(text("SELECT 1 FROM order_ref WHERE order_key=:k"), {"k": order_key}).fetchone()
    if exists:
        conn.execute(text("""UPDATE order_ref SET total_minor=:t, currency=:c, last_source_updated_at=:lu, updated_at=:u,
                             order_number=COALESCE(:on, order_number) WHERE order_key=:k"""),
                     {"t": total, "c": currency, "lu": body.get("updated_at"), "u": now_iso(), "on": body.get("name"), "k": order_key})
        if source_kind == "order":
            # Shopify financial status is authoritative for Shopify-checkout payments (gateway-verified by Shopify);
            # for payment links we rely on Razorpay webhooks. Apply monotonic rule.
            cur = conn.execute(text("SELECT financial_state FROM order_ref WHERE order_key=:k"), {"k": order_key}).fetchone()[0]
            if payments.RANK.get(fin, 0) >= payments.RANK.get(cur, 0) or fin in ("refunded", "partially_refunded"):
                conn.execute(text("UPDATE order_ref SET financial_state=:f, payment_mode=:pm, paid_minor=CASE WHEN :f='paid' THEN total_minor ELSE paid_minor END WHERE order_key=:k"),
                             {"f": fin, "pm": payment_mode, "k": order_key})
    else:
        conn.execute(
            text("""INSERT INTO order_ref(order_key, brand_id, source, source_order_id, order_number, contact_id, currency, total_minor,
                    paid_minor, financial_state, fulfilment_state, payment_mode, last_source_updated_at, updated_at)
                    VALUES (:k,:b,'shopify',:s,:on,:c,:cur,:t,:p,:f,'unfulfilled',:pm,:lu,:u)"""),
            {"k": order_key, "b": brand_id, "s": sid, "on": body.get("name"), "c": contact_id, "cur": currency, "t": total,
             "p": total if fin == "paid" else 0, "f": fin, "pm": payment_mode, "lu": body.get("updated_at"), "u": now_iso()},
        )
    return order_key


def handle(conn: Connection, ev: InternalEvent, first_delay_min: int = 45, shopify_client=None) -> list[str]:
    """Returns a list of human-readable actions taken. Idempotent per event (caller checks inbox duplicate)."""
    actions: list[str] = []
    b = ev.brand_id
    p = ev.payload

    if ev.event_type in ("shopify.checkouts.create", "shopify.checkouts.update"):
        if ev.auth_level != "verified_signature":
            return ["ignored: unverified shopify event"]
        key = upsert_order_from_shopify(conn, b, p, "checkout")
        if p.get("completed_at"):
            n = outbox.cancel_where(conn, "checkout completed", order_key=key, category="promotional")
            actions.append(f"checkout completed; cancelled {n} promotional jobs")
            return actions
        contact_id = conn.execute(text("SELECT contact_id FROM order_ref WHERE order_key=:k"), {"k": key}).fetchone()[0]
        if not contact_id:
            return ["no contact linkage; no reminder queued"]
        run_after = (datetime.now(timezone.utc) + timedelta(minutes=first_delay_min)).isoformat()
        expires = (datetime.now(timezone.utc) + timedelta(hours=36)).isoformat()
        _, created = outbox.enqueue(conn, b, f"{key}:abandoned_reminder_1", "send_message", "promotional",
                                    {"purpose": "abandoned_checkout", "channel_type": "whatsapp", "template": "amrit_checkout_help_v1",
                                     "est_cost_minor": 100}, run_after, contact_id, key, expires)
        actions.append("queued abandoned_reminder_1" if created else "reminder already queued (idempotent)")
        return actions

    if ev.event_type in ("shopify.orders.create", "shopify.orders.paid", "shopify.orders.updated"):
        if ev.auth_level != "verified_signature":
            return ["ignored: unverified shopify event"]
        key = upsert_order_from_shopify(conn, b, p, "order")
        ck = p.get("checkout_id")
        if ck:
            n = outbox.cancel_where(conn, "order created from checkout", order_key=f"{b}:shopify:checkout:{ck}", category="promotional")
            actions.append(f"cancelled {n} abandonment jobs for checkout {ck}")
        fin = conn.execute(text("SELECT financial_state FROM order_ref WHERE order_key=:k"), {"k": key}).fetchone()[0]
        actions.append(f"order {p.get('name')} state={fin}; confirmation owner=Shopify email (unchanged until F10 approved)")
        if ev.event_type == "shopify.orders.paid" and shopify_client is not None:
            from .engine.state import on_order_paid
            res = on_order_paid(conn, {"id": p.get("admin_graphql_api_id") or f"gid://shopify/Order/{p.get('id')}",
                                       "draft_order": p.get("draft_order"), "draft_order_id": p.get("draft_order_id"),
                                       "note_attributes": p.get("note_attributes"), "tags": p.get("tags")}, shopify_client)
            actions.append(f"engine: {res.get('status')}")
            if res.get("status") == "ordered":
                outbox.enqueue(conn, b, f"{key}:engine_order_confirm", "send_message", "service",
                               {"purpose": "order_confirm", "channel_type": "whatsapp", "message": res["message"], "phone": res["phone"]}, now_iso(),
                               conn.execute(text("SELECT contact_id FROM order_ref WHERE order_key=:k"), {"k": key}).fetchone()[0], key)
        return actions

    if ev.event_type.startswith("razorpay.payment_link."):
        if ev.auth_level != "verified_signature":
            return ["ignored: unverified razorpay event"]
        pl = (p.get("payload", {}).get("payment_link") or {}).get("entity") or {}
        pay = (p.get("payload", {}).get("payment") or {}).get("entity") or {}
        ref = pl.get("reference_id") or ""
        order_key = ref if ref.startswith(b + ":") else None
        if not order_key:
            audit(conn, "adapter", "razorpay.unlinked", pl.get("id"), {"reference_id": ref})
            return ["payment link not linked to an order_key reference_id -> payment_review"]
        if ev.event_type in ("razorpay.payment_link.paid", "razorpay.payment_link.partially_paid") and pay.get("id"):
            r = payments.record_attempt(conn, order_key, "razorpay", pay["id"], int(pay.get("amount", 0)), pay.get("currency", "INR"),
                                        "captured" if pay.get("status") == "captured" else "created", True, str(pay.get("created_at")), pl.get("id"))
            actions.append(f"payment {pay['id']} -> {r.financial_state} ({r.reason})")
            if r.financial_state in ("paid", "partially_paid"):
                n = outbox.cancel_where(conn, "payment received", order_key=order_key, category="promotional")
                actions.append(f"cancelled {n} promotional jobs")
        elif ev.event_type in ("razorpay.payment_link.expired", "razorpay.payment_link.cancelled"):
            actions.append(f"link {pl.get('id')} {ev.event_type.split('.')[-1]}; no state change (order stays as-is)")
        return actions

    if ev.event_type == "razorpay.payment.failed":
        pay = (p.get("payload", {}).get("payment") or {}).get("entity") or {}
        notes = pay.get("notes") or {}
        order_key = notes.get("order_key")
        if ev.auth_level == "verified_signature" and order_key and pay.get("id"):
            r = payments.record_attempt(conn, order_key, "razorpay", pay["id"], int(pay.get("amount", 0)), pay.get("currency", "INR"),
                                        "failed", True, str(pay.get("created_at")))
            actions.append(f"failed attempt recorded; order state {r.financial_state} ({r.reason})")
        return actions or ["failed payment without order linkage ignored"]

    if ev.event_type == "shiprocket.tracking":
        awb = str(p.get("awb"))
        sr_order = str(p.get("sr_order_id") or p.get("order_id") or "")
        key = f"{b}:shiprocket:{awb}"
        order_key = _order_key_for_channel_order(conn, b, str(p.get("order_id") or ""))
        if not order_key:
            audit(conn, "adapter", "shiprocket.unlinked", awb, {"order_id": p.get("order_id")})
            return [f"shiprocket order_id {p.get('order_id')} not linked to a known order; internal task"]
        shipments.ensure_shipment(conn, key, order_key, awb, p.get("courier_name"))
        upd = shipments.apply_event(conn, key, p.get("current_status") or "", str(p.get("current_status_id") or ""),
                                    str(p.get("current_timestamp") or ""), ev.auth_level)
        actions.append(f"shipment {awb}: {upd.previous_state} -> {upd.new_state} ({upd.reason}); customer_notify={upd.customer_notify}")
        if upd.applied and upd.new_state in shipments.CUSTOMER_VISIBLE and not upd.customer_notify:
            # static-token hint: queue a verification job instead of a customer message
            outbox.enqueue(conn, b, f"{key}:{upd.new_state}:verify", "internal_task", "internal",
                           {"purpose": "verify_shipment_via_api", "awb": awb, "state": upd.new_state}, now_iso(), None, order_key)
            actions.append("queued authenticated re-verification before any customer notification")
        return actions

    return [f"no handler for {ev.event_type}"]


def _order_key_for_channel_order(conn: Connection, brand_id: str, channel_order_id: str) -> str | None:
    if not channel_order_id:
        return None
    row = conn.execute(text("SELECT order_key FROM order_ref WHERE brand_id=:b AND (order_number=:n OR source_order_id=:n)"),
                       {"b": brand_id, "n": channel_order_id}).fetchone()
    return row[0] if row else None
