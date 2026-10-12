"""Payment reconciliation.

- A payment attempt is 'verified' only if it arrived via a signature-verified webhook OR was confirmed by an
  authenticated provider API fetch. Customer text, screenshots, typed transaction ids: never verified.
- Monotonic: a late 'failed' event for an older attempt never downgrades an order that has a verified capture.
- Sums reconcile to the order currency/amount. Partial stays partial until the approved threshold (which must
  exist in policy; none is invented here).
- Idempotent: attempt_key = provider:payment_id. Replays return the existing state.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import audit, now_iso

RANK = {"unpaid": 0, "cod_pending": 0, "partially_paid": 1, "paid": 2, "partially_refunded": 3, "refunded": 4, "disputed": 5}


class PaymentRejected(Exception):
    pass


@dataclass(frozen=True)
class ReconcileResult:
    order_key: str
    financial_state: str
    paid_minor: int
    applied: bool
    reason: str


def _order(conn: Connection, order_key: str):
    row = conn.execute(text("SELECT order_key, currency, total_minor, paid_minor, financial_state, fulfilment_state FROM order_ref WHERE order_key=:k"), {"k": order_key}).fetchone()
    if not row:
        raise PaymentRejected(f"unknown order {order_key}")
    return row


def record_attempt(conn: Connection, order_key: str, provider: str, payment_id: str, amount_minor: int, currency: str,
                   status: str, verified: bool, occurred_at: str, link_or_order_id: str | None = None) -> ReconcileResult:
    o = _order(conn, order_key)
    attempt_key = f"{provider}:{payment_id}"
    existing = conn.execute(text("SELECT status FROM payment_attempt WHERE attempt_key=:k"), {"k": attempt_key}).fetchone()
    if existing:
        # Replay / duplicate: do not re-apply. If status changed from created->captured for the same id, update once.
        if existing[0] == status:
            return ReconcileResult(order_key, o.financial_state, o.paid_minor, False, "duplicate attempt event ignored")
        conn.execute(text("UPDATE payment_attempt SET status=:s, verified=:v, occurred_at=:oa WHERE attempt_key=:k"),
                     {"s": status, "v": int(verified), "oa": occurred_at, "k": attempt_key})
    else:
        conn.execute(
            text("""INSERT INTO payment_attempt(attempt_key, order_key, provider, provider_payment_id, provider_order_or_link_id,
                    amount_minor, currency, status, verified, occurred_at, received_at)
                    VALUES (:k,:o,:p,:pid,:lid,:a,:c,:s,:v,:oa,:ra)"""),
            {"k": attempt_key, "o": order_key, "p": provider, "pid": payment_id, "lid": link_or_order_id, "a": amount_minor,
             "c": currency, "s": status, "v": int(verified), "oa": occurred_at, "ra": now_iso()},
        )
    return recompute(conn, order_key)


def recompute(conn: Connection, order_key: str) -> ReconcileResult:
    """Derive financial state from VERIFIED captured attempts only. Never downgrade on rank."""
    o = _order(conn, order_key)
    rows = conn.execute(
        text("SELECT amount_minor, currency, status, verified FROM payment_attempt WHERE order_key=:k"), {"k": order_key}
    ).fetchall()
    captured = sum(r.amount_minor for r in rows if r.status == "captured" and r.verified and r.currency == o.currency)
    refunded = sum(r.amount_minor for r in rows if r.status == "refunded" and r.verified and r.currency == o.currency)
    disputed = any(r.status == "disputed" and r.verified for r in rows)
    mismatch_currency = any(r.currency != o.currency and r.status == "captured" and r.verified for r in rows)

    if mismatch_currency:
        new_state, reason = o.financial_state, "currency mismatch on verified capture -> payment_review"
        _open_case(conn, order_key, "payment_review", reason)
        return ReconcileResult(order_key, new_state, o.paid_minor, False, reason)

    paid = captured - refunded
    if disputed:
        new_state = "disputed"
    elif captured == 0:
        new_state = o.financial_state if o.financial_state in ("unpaid", "cod_pending") else o.financial_state
    elif refunded >= captured:
        new_state = "refunded"
    elif refunded > 0:
        new_state = "partially_refunded"
    elif captured > o.total_minor:
        _open_case(conn, order_key, "payment_review", "overpayment: captured exceeds order total")
        new_state = "paid"
    elif captured == o.total_minor:
        new_state = "paid"
    else:
        new_state = "partially_paid"

    applied = True
    reason = "recomputed from verified attempts"
    if RANK.get(new_state, 0) < RANK.get(o.financial_state, 0) and new_state not in ("refunded", "partially_refunded", "disputed"):
        # never downgrade paid -> partially/unpaid from a late/older event
        applied, reason, new_state, paid = False, "downgrade suppressed (monotonic)", o.financial_state, max(o.paid_minor, paid)
    conn.execute(text("UPDATE order_ref SET financial_state=:f, paid_minor=:p, updated_at=:u WHERE order_key=:k"),
                 {"f": new_state, "p": paid, "u": now_iso(), "k": order_key})
    audit(conn, "adapter", "payment.recompute", order_key, {"state": new_state, "paid_minor": paid, "applied": applied})
    return ReconcileResult(order_key, new_state, paid, applied, reason)


def fulfilment_eligible(conn: Connection, order_key: str, approved_partial_threshold_pct: int | None) -> tuple[bool, str]:
    """Approved threshold must come from policy. None => only fully paid prepaid or approved COD is eligible."""
    o = _order(conn, order_key)
    pm = conn.execute(text("SELECT payment_mode, cod_decision FROM order_ref WHERE order_key=:k"), {"k": order_key}).fetchone()
    if o.financial_state == "paid":
        return True, "fully paid (verified)"
    if pm.payment_mode == "cod" and (pm.cod_decision or "").startswith("approved_zone_"):
        return True, f"approved COD ({pm.cod_decision})"
    if o.financial_state == "partially_paid" and approved_partial_threshold_pct is not None:
        if o.total_minor and (o.paid_minor * 100) // o.total_minor >= approved_partial_threshold_pct:
            return True, f"partial payment meets approved threshold {approved_partial_threshold_pct}%"
        return False, "partial payment below approved threshold"
    return False, f"not eligible: financial_state={o.financial_state}, payment_mode={pm.payment_mode}, cod_decision={pm.cod_decision}"


def _open_case(conn: Connection, order_key: str, case_type: str, reason: str) -> None:
    from .db import new_id
    conn.execute(
        text("""INSERT INTO support_case(case_id, brand_id, contact_id, order_key, case_type, owner_team, priority, status, created_at, updated_at)
                SELECT :i, brand_id, contact_id, order_key, :t, 'payment_review', 'high', 'open', :n, :n FROM order_ref WHERE order_key=:k"""),
        {"i": new_id(), "t": case_type, "n": now_iso(), "k": order_key},
    )
    audit(conn, "adapter", "case.opened", order_key, {"type": case_type, "reason": reason})
