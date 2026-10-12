"""Internal exception / operating reports (F40). PII-free: counts and reference keys only.
Disabled by default (REPORTS_ENABLED=false) until destination, schedule and recipients are approved."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import budget


def daily_exceptions(conn: Connection, brand_id: str) -> dict:
    q = lambda sql, **p: conn.execute(text(sql), {"b": brand_id, **p}).scalar() or 0  # noqa: E731
    return {
        "brand_id": brand_id,
        "unpaid_prepaid_orders": q("SELECT COUNT(*) FROM order_ref WHERE brand_id=:b AND payment_mode='prepaid' AND financial_state='unpaid'"),
        "partially_paid_orders": q("SELECT COUNT(*) FROM order_ref WHERE brand_id=:b AND financial_state='partially_paid'"),
        "cod_manual_review": q("SELECT COUNT(*) FROM order_ref WHERE brand_id=:b AND cod_decision='manual_review'"),
        "open_ndr_cases": q("SELECT COUNT(*) FROM support_case WHERE brand_id=:b AND case_type='ndr' AND status='open'"),
        "open_payment_review": q("SELECT COUNT(*) FROM support_case WHERE brand_id=:b AND case_type='payment_review' AND status='open'"),
        "unassigned_open_cases": q("SELECT COUNT(*) FROM support_case WHERE brand_id=:b AND status='open' AND owner_team IS NULL"),
        "shipments_label_only_over_48h": q("""SELECT COUNT(*) FROM shipment_ref s JOIN order_ref o ON o.order_key=s.order_key
                                             WHERE o.brand_id=:b AND s.state IN ('label_created','pickup_scheduled') AND s.updated_at < :cut""",
                                           cut=_hours_ago(48)),
        "dead_letter_jobs": q("SELECT COUNT(*) FROM outbox_job WHERE brand_id=:b AND status='dead_letter'"),
        "reconcile_required_jobs": q("SELECT COUNT(*) FROM outbox_job WHERE brand_id=:b AND status='reconcile_required'"),
        "promotional_sent_today": q("SELECT COUNT(*) FROM touch_log WHERE brand_id=:b AND category='promotional' AND sent_at >= :cut", cut=_hours_ago(24)),
        "wallet_runway_days": budget.runway_days(conn, brand_id),
    }


def _hours_ago(h: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
