"""MOCK end-to-end: checkout abandoned -> reminder queued -> order paid via Shopify webhook -> reminder cancelled;
Razorpay payment link paid -> reconciled; Shiprocket tracking (static token) -> verification job, no customer send.
Also T31/T32 cow reservation concurrency on a minimal registry table (adapter-local, reference only)."""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from amrit_adapter import consent, handlers, outbox
from amrit_adapter.envelope import InternalEvent, persist_event
from amrit_adapter.sender import DryRunSender
from tests.conftest import BRAND

NOW = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc)


def ev(source, sid, etype, payload, auth="verified_signature"):
    return InternalEvent(brand_id=BRAND, source=source, source_event_id=sid, event_type=etype, occurred_at="2026-10-12T00:00:00Z",
                         auth_level=auth, payload=payload)


def test_e2e_checkout_to_paid_cancels_reminder(engine):
    with engine.begin() as c:
        checkout = {"id": 900, "token": "tok", "total_price": "3500.00", "currency": "INR", "customer": {"id": 42}, "completed_at": None}
        a = handlers.handle(c, ev("shopify", "ck-1", "shopify.checkouts.update", checkout), first_delay_min=45)
        assert a == ["queued abandoned_reminder_1"]
        a2 = handlers.handle(c, ev("shopify", "ck-2", "shopify.checkouts.update", checkout), first_delay_min=45)
        assert a2 == ["reminder already queued (idempotent)"]
        order = {"id": 6563739599031, "name": "#1150", "checkout_id": 900, "total_price": "3500.00", "currency": "INR",
                 "financial_status": "paid", "payment_gateway_names": ["01 cards, upi, nb, wallets by razorpay"], "customer": {"id": 42}}
        a3 = handlers.handle(c, ev("shopify", "ord-1", "shopify.orders.paid", order))
        assert a3[0] == "cancelled 1 abandonment jobs for checkout 900"
        assert c.execute(text("SELECT status, cancel_reason FROM outbox_job")).fetchone() == ("cancelled", "order created from checkout")
        row = c.execute(text("SELECT financial_state, payment_mode, paid_minor FROM order_ref WHERE order_number='#1150'")).fetchone()
        assert row == ("paid", "prepaid", 350000)


def test_e2e_unverified_shopify_event_is_ignored(engine):
    with engine.begin() as c:
        a = handlers.handle(c, ev("shopify", "ck-x", "shopify.checkouts.update", {"id": 1, "customer": {"id": 1}}, auth="unauthenticated"))
        assert a == ["ignored: unverified shopify event"]


def test_e2e_razorpay_link_paid_reconciles_and_cancels_promos(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO contact(contact_id, brand_id, created_at) VALUES ('c9', :b, '2026-10-12')"), {"b": BRAND})
        key = f"{BRAND}:shopify:order:555"
        c.execute(text("""INSERT INTO order_ref(order_key, brand_id, source, source_order_id, contact_id, currency, total_minor, paid_minor,
                          financial_state, fulfilment_state, payment_mode, updated_at) VALUES (:k,:b,'shopify','555','c9','INR',150000,0,'unpaid','unfulfilled','prepaid','x')"""),
                  {"k": key, "b": BRAND})
        outbox.enqueue(c, BRAND, f"{key}:payment_nudge", "send_message", "promotional", {"purpose": "abandoned_checkout"}, NOW.isoformat(), "c9", key)
        payload = {"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"id": "plink_1", "reference_id": key, "status": "paid"}},
            "payment": {"entity": {"id": "pay_Z", "amount": 150000, "currency": "INR", "status": "captured", "created_at": 1760000000}}}}
        a = handlers.handle(c, ev("razorpay", "rzp-1", "razorpay.payment_link.paid", payload))
        assert a == ["payment pay_Z -> paid (recomputed from verified attempts)", "cancelled 1 promotional jobs"]
        # replay of the same provider event at the inbox layer is a duplicate
        r1 = persist_event(c, ev("razorpay", "rzp-1", "razorpay.payment_link.paid", payload))
        r2 = persist_event(c, ev("razorpay", "rzp-1", "razorpay.payment_link.paid", payload))
        assert r2.duplicate


def test_e2e_shiprocket_hint_never_notifies_customer_directly(engine):
    with engine.begin() as c:
        key = f"{BRAND}:shopify:order:777"
        c.execute(text("""INSERT INTO order_ref(order_key, brand_id, source, source_order_id, order_number, currency, total_minor, paid_minor,
                          financial_state, fulfilment_state, payment_mode, updated_at) VALUES (:k,:b,'shopify','777','#1151','INR',1,0,'paid','unfulfilled','prepaid','x')"""),
                  {"k": key, "b": BRAND})
        payload = {"awb": "AWB777", "order_id": "#1151", "current_status": "PICKED UP", "current_status_id": 42, "current_timestamp": "2026-10-12 10:00:00", "courier_name": "X"}
        a = handlers.handle(c, ev("shiprocket", "AWB777:42:t", "shiprocket.tracking", payload, auth="static_token_hint"))
        assert "customer_notify=False" in a[0] and a[1].startswith("queued authenticated re-verification")
        jobs = c.execute(text("SELECT job_type, category FROM outbox_job")).fetchall()
        assert jobs == [("internal_task", "internal")]


def test_T31_T32_cow_reservation_is_atomic_and_history_kept(engine):
    """Minimal registry contract the external cow registry must satisfy (reference implementation on SQLite)."""
    with engine.begin() as c:
        c.execute(text("CREATE TABLE cow (cow_id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL)"))
        c.execute(text("CREATE TABLE cow_history (cow_id TEXT, from_state TEXT, to_state TEXT, at TEXT)"))
        c.execute(text("INSERT INTO cow VALUES ('COW-001','available',1)"))

        def reserve(expected_version):
            n = c.execute(text("UPDATE cow SET state='reserved', version=version+1 WHERE cow_id='COW-001' AND state='available' AND version=:v"),
                          {"v": expected_version}).rowcount
            if n:
                c.execute(text("INSERT INTO cow_history VALUES ('COW-001','available','reserved','2026-10-12')"))
            return bool(n)

        assert reserve(1) is True
        assert reserve(1) is False          # second concurrent attempt with stale version fails
        c.execute(text("UPDATE cow SET state='sold', version=version+1 WHERE cow_id='COW-001' AND state='reserved'"))
        c.execute(text("INSERT INTO cow_history VALUES ('COW-001','reserved','sold','2026-10-13')"))
        assert c.execute(text("SELECT COUNT(*) FROM cow_history")).scalar() == 2
        assert c.execute(text("SELECT state FROM cow WHERE cow_id='COW-001'")).scalar() == "sold"
