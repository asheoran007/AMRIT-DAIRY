"""Outbox / job queue with pre-send rechecks, bounded retries, dead-letter and reconcile-required states."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import budget as budget_mod
from .consent import has_consent
from .db import audit, new_id, now_iso
from .sender import Sender, SendResult

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Policy:
    brand_id: str
    promo_window_start: str = "09:00"
    promo_window_end: str = "20:00"
    promo_max_per_24h: int = 1
    promo_max_per_7d: int = 2
    reserve_minor: int = 0
    monthly_cap_minor: int | None = None
    kill_switch: bool = True
    max_attempts: int = 5


def enqueue(conn: Connection, brand_id: str, action_key: str, job_type: str, category: str, payload: dict,
            run_after: str, contact_id: str | None = None, order_key: str | None = None,
            expires_at: str | None = None, max_attempts: int = 5) -> tuple[str, bool]:
    """Returns (job_id, created). Duplicate action_key => existing job, nothing new."""
    row = conn.execute(text("SELECT job_id FROM outbox_job WHERE action_key=:k"), {"k": action_key}).fetchone()
    if row:
        return row[0], False
    job_id = new_id()
    conn.execute(
        text("""INSERT INTO outbox_job(job_id, brand_id, action_key, job_type, category, contact_id, order_key, payload_json,
                run_after, expires_at, attempts, max_attempts, status, created_at, updated_at)
                VALUES (:j,:b,:k,:t,:c,:ct,:o,:p,:r,:e,0,:m,'queued',:n,:n)"""),
        {"j": job_id, "b": brand_id, "k": action_key, "t": job_type, "c": category, "ct": contact_id, "o": order_key,
         "p": json.dumps(payload, default=str), "r": run_after, "e": expires_at, "m": max_attempts, "n": now_iso()},
    )
    return job_id, True


def cancel_where(conn: Connection, reason: str, order_key: str | None = None, contact_id: str | None = None,
                 category: str | None = None) -> int:
    """Cancel queued jobs matching the filters (purchase completed, opt-out, complaint, human takeover, budget stop)."""
    clauses, params = ["status='queued'"], {"r": reason, "n": now_iso()}
    if order_key:
        clauses.append("order_key=:o"); params["o"] = order_key
    if contact_id:
        clauses.append("contact_id=:c"); params["c"] = contact_id
    if category:
        clauses.append("category=:cat"); params["cat"] = category
    res = conn.execute(text(f"UPDATE outbox_job SET status='cancelled', cancel_reason=:r, updated_at=:n WHERE {' AND '.join(clauses)}"), params)
    return res.rowcount or 0


# ---------------- pre-send rechecks ----------------

def _in_promo_window(now_local: datetime, start: str, end: str) -> bool:
    hhmm = now_local.strftime("%H:%M")
    return start <= hhmm < end


def _touches(conn: Connection, brand_id: str, contact_id: str, category: str, since_iso: str) -> int:
    return conn.execute(
        text("SELECT COUNT(*) FROM touch_log WHERE brand_id=:b AND contact_id=:c AND category=:cat AND sent_at>=:s"),
        {"b": brand_id, "c": contact_id, "cat": category, "s": since_iso},
    ).scalar() or 0


def recheck(conn: Connection, job, policy: Policy, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Returns (ok, reason). Any failure is a reason to cancel or defer, never to send."""
    now_utc = now_utc or datetime.now(timezone.utc)
    payload = json.loads(job.payload_json)
    if job.expires_at and now_utc.isoformat() > job.expires_at:
        return False, "expired"
    if job.order_key:
        o = conn.execute(text("SELECT financial_state, fulfilment_state, human_owner FROM order_ref WHERE order_key=:k"), {"k": job.order_key}).fetchone()
        if o:
            if o.human_owner:
                return False, "human owns the case (bot/promos paused)"
            if job.category == "promotional" and job.job_type == "send_message" and payload.get("purpose") == "abandoned_checkout" \
                    and o.financial_state in ("paid", "partially_paid") :
                return False, "order already paid"
            if o.fulfilment_state == "cancelled" and job.category == "promotional":
                return False, "order cancelled"
    if job.contact_id:
        open_complaint = conn.execute(
            text("SELECT 1 FROM support_case WHERE contact_id=:c AND status='open' AND case_type IN ('complaint','medical','refund','payment_review') LIMIT 1"),
            {"c": job.contact_id},
        ).fetchone()
        if job.category == "promotional" and open_complaint:
            return False, "open sensitive case suppresses promotions"
        purpose = "marketing" if job.category == "promotional" else "service"
        channel = payload.get("channel_type", "whatsapp")
        if not has_consent(conn, policy.brand_id, job.contact_id, channel, purpose):
            return False, f"no {purpose} consent on {channel}"
        if job.category == "promotional":
            local = now_utc.astimezone(IST)
            if not _in_promo_window(local, policy.promo_window_start, policy.promo_window_end):
                return False, "outside promotional window"
            if _touches(conn, policy.brand_id, job.contact_id, "promotional", (now_utc - timedelta(hours=24)).isoformat()) >= policy.promo_max_per_24h:
                return False, "24h promotional cap reached (all channels)"
            if _touches(conn, policy.brand_id, job.contact_id, "promotional", (now_utc - timedelta(days=7)).isoformat()) >= policy.promo_max_per_7d:
                return False, "7d promotional cap reached (all channels)"
    est = int(payload.get("est_cost_minor", 0))
    b = budget_mod.state(conn, policy.brand_id, policy.reserve_minor, policy.monthly_cap_minor, policy.kill_switch, now_utc.strftime("%Y-%m"))
    ok, why = b.allows(job.category, est)
    if not ok:
        return False, f"budget: {why}"
    return True, "ok"


# ---------------- worker ----------------

def run_due(conn: Connection, sender: Sender, policy: Policy, now_utc: datetime | None = None, limit: int = 50) -> list[dict]:
    """Process due jobs once. Claims by status transition to avoid concurrent double execution."""
    now_utc = now_utc or datetime.now(timezone.utc)
    results: list[dict] = []
    due = conn.execute(
        text("SELECT * FROM outbox_job WHERE status='queued' AND run_after<=:n ORDER BY run_after LIMIT :l"),
        {"n": now_utc.isoformat(), "l": limit},
    ).fetchall()
    for job in due:
        claimed = conn.execute(
            text("UPDATE outbox_job SET status='running', updated_at=:u WHERE job_id=:j AND status='queued'"),
            {"u": now_iso(), "j": job.job_id},
        ).rowcount
        if not claimed:
            continue  # another worker took it
        ok, reason = recheck(conn, job, policy, now_utc)
        if not ok:
            deferrable = reason in ("outside promotional window",) or reason.startswith("budget: wallet")
            new_status = "queued" if deferrable else "cancelled"
            run_after = (now_utc + timedelta(minutes=30)).isoformat() if deferrable else job.run_after
            conn.execute(text("UPDATE outbox_job SET status=:s, cancel_reason=:r, run_after=:ra, updated_at=:u WHERE job_id=:j"),
                         {"s": new_status, "r": reason, "ra": run_after, "u": now_iso(), "j": job.job_id})
            audit(conn, "worker", "job.recheck_failed", job.action_key, {"reason": reason, "status": new_status})
            results.append({"action_key": job.action_key, "status": new_status, "reason": reason})
            continue
        payload = json.loads(job.payload_json)
        # exactly-one-intended-action guard: if a ledger row exists, never execute again
        if conn.execute(text("SELECT 1 FROM action_ledger WHERE action_key=:k"), {"k": job.action_key}).fetchone():
            conn.execute(text("UPDATE outbox_job SET status='done', updated_at=:u WHERE job_id=:j"), {"u": now_iso(), "j": job.job_id})
            results.append({"action_key": job.action_key, "status": "done", "reason": "already executed"})
            continue
        res: SendResult = sender.send(job.action_key, payload)
        attempts = job.attempts + 1
        if res.outcome in ("sent", "dry_run"):
            conn.execute(text("""INSERT INTO action_ledger(action_key, job_id, provider, provider_object_id, outcome, cost_minor, executed_at)
                                 VALUES (:k,:j,'bitespeed',:p,:o,:c,:t)"""),
                         {"k": job.action_key, "j": job.job_id, "p": res.provider_object_id, "o": res.outcome, "c": res.cost_minor, "t": now_iso()})
            conn.execute(text("UPDATE outbox_job SET status='done', attempts=:a, updated_at=:u WHERE job_id=:j"),
                         {"a": attempts, "u": now_iso(), "j": job.job_id})
            if job.contact_id and job.job_type == "send_message":
                conn.execute(text("INSERT INTO touch_log(touch_id, brand_id, contact_id, category, channel_type, sent_at) VALUES (:i,:b,:c,:cat,:ch,:t)"),
                             {"i": new_id(), "b": job.brand_id, "c": job.contact_id, "cat": job.category,
                              "ch": payload.get("channel_type", "whatsapp"), "t": now_utc.isoformat()})
            if res.cost_minor:
                budget_mod.post(conn, job.brand_id, "charge_estimate", -res.cost_minor, job.category, job.action_key)
            results.append({"action_key": job.action_key, "status": "done", "reason": res.outcome})
        elif res.outcome == "unknown_timeout":
            # Do NOT retry blindly: reconcile against provider ids first (T19).
            conn.execute(text("UPDATE outbox_job SET status='reconcile_required', attempts=:a, last_error=:e, updated_at=:u WHERE job_id=:j"),
                         {"a": attempts, "e": res.detail, "u": now_iso(), "j": job.job_id})
            results.append({"action_key": job.action_key, "status": "reconcile_required", "reason": res.detail})
        else:
            if attempts >= job.max_attempts:
                conn.execute(text("UPDATE outbox_job SET status='dead_letter', attempts=:a, last_error=:e, updated_at=:u WHERE job_id=:j"),
                             {"a": attempts, "e": res.detail, "u": now_iso(), "j": job.job_id})
                _internal_task(conn, job, f"dead-letter after {attempts} attempts: {res.detail}")
                results.append({"action_key": job.action_key, "status": "dead_letter", "reason": res.detail})
            else:
                backoff = min(2 ** attempts, 60) * 60 + random.randint(0, 120)
                conn.execute(text("UPDATE outbox_job SET status='queued', attempts=:a, last_error=:e, run_after=:r, updated_at=:u WHERE job_id=:j"),
                             {"a": attempts, "e": res.detail, "r": (now_utc + timedelta(seconds=backoff)).isoformat(), "u": now_iso(), "j": job.job_id})
                results.append({"action_key": job.action_key, "status": "retry", "reason": res.detail})
    return results


def reconcile(conn: Connection, action_key: str, provider_object_id: str | None) -> str:
    """Operator/tool-driven reconciliation of a reconcile_required job after an ambiguous timeout.
    provider_object_id given => the provider did execute: record ledger, mark done. None => safe to requeue once."""
    job = conn.execute(text("SELECT * FROM outbox_job WHERE action_key=:k"), {"k": action_key}).fetchone()
    if not job or job.status != "reconcile_required":
        return "nothing to reconcile"
    if provider_object_id:
        conn.execute(text("""INSERT INTO action_ledger(action_key, job_id, provider, provider_object_id, outcome, cost_minor, executed_at)
                             VALUES (:k,:j,'bitespeed',:p,'sent',0,:t)"""), {"k": action_key, "j": job.job_id, "p": provider_object_id, "t": now_iso()})
        conn.execute(text("UPDATE outbox_job SET status='done', updated_at=:u WHERE job_id=:j"), {"u": now_iso(), "j": job.job_id})
        return "done (provider had executed)"
    conn.execute(text("UPDATE outbox_job SET status='queued', run_after=:r, updated_at=:u WHERE job_id=:j"), {"r": now_iso(), "u": now_iso(), "j": job.job_id})
    return "requeued (provider confirmed not executed)"


def _internal_task(conn: Connection, job, reason: str) -> None:
    conn.execute(
        text("""INSERT INTO support_case(case_id, brand_id, contact_id, order_key, case_type, owner_team, priority, status, created_at, updated_at)
                VALUES (:i,:b,:c,:o,'other','technical','high','open',:n,:n)"""),
        {"i": new_id(), "b": job.brand_id, "c": job.contact_id, "o": job.order_key, "n": now_iso()},
    )
    audit(conn, "worker", "job.dead_letter", job.action_key, {"reason": reason})
