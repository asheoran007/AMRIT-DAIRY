"""T04/T15/T19/T24/T25/T27/T38/T40/T41/T42: consent, idempotent actions, reconcile-before-retry, human takeover,
complaint suppression, cancellation, cross-channel caps, budget guard, dead-letter, restart safety."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from amrit_adapter import budget, consent, outbox
from amrit_adapter.sender import DryRunSender, FlakySender, SendResult
from tests.conftest import BRAND, mk_contact, mk_order

# 2026-10-12 12:00 IST (inside the 09:00-20:00 window)
NOW = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc)


def policy(**kw):
    base = dict(brand_id=BRAND, reserve_minor=200000, monthly_cap_minor=500000, kill_switch=False)
    base.update(kw)
    return outbox.Policy(**base)


def seed(c, consent_ok=True, topup=1000000):
    mk_contact(c); k = mk_order(c)
    if consent_ok:
        consent.record_consent(c, BRAND, "c1", "whatsapp", "marketing", "granted", "optin-form-ref")
    budget.post(c, BRAND, "topup", topup, None, "E02 top-up (test fixture)")
    return k


def promo(c, k, key="amrit_dairy:shopify:order:1:abandoned_reminder_1"):
    return outbox.enqueue(c, BRAND, key, "send_message", "promotional",
                          {"purpose": "abandoned_checkout", "channel_type": "whatsapp", "est_cost_minor": 100},
                          NOW.isoformat(), "c1", k, (NOW + timedelta(hours=36)).isoformat())


def test_T15_duplicate_action_key_never_creates_second_job_or_send(engine):
    with engine.begin() as c:
        k = seed(c)
        j1, created1 = promo(c, k); j2, created2 = promo(c, k)
        assert created1 and not created2 and j1 == j2
        s = DryRunSender()
        outbox.run_due(c, s, policy(), NOW); outbox.run_due(c, s, policy(), NOW)
        assert len(s.log) == 1


def test_T04_withdrawn_consent_suppresses_at_dispatch_time(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        consent.record_consent(c, BRAND, "c1", "whatsapp", "marketing", "withdrawn", "STOP message ref")
        s = DryRunSender(); r = outbox.run_due(c, s, policy(), NOW)
        assert r[0]["status"] == "cancelled" and "consent" in r[0]["reason"] and s.log == []


def test_T04_stop_all_wins_over_any_purpose(engine):
    with engine.begin() as c:
        k = seed(c)
        consent.record_consent(c, BRAND, "c1", "whatsapp", "service", "granted", "x")
        consent.record_consent(c, BRAND, "c1", "email", "marketing", "stop_all", "x")
        assert consent.has_consent(c, BRAND, "c1", "whatsapp", "service") is False


def test_T27_purchase_cancels_reminder(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        c.execute(text("UPDATE order_ref SET financial_state='paid', paid_minor=total_minor WHERE order_key=:k"), {"k": k})
        s = DryRunSender(); r = outbox.run_due(c, s, policy(), NOW)
        assert r[0]["status"] == "cancelled" and r[0]["reason"] == "order already paid"


def test_T24_human_takeover_pauses_promos(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        c.execute(text("UPDATE order_ref SET human_owner='agent:priya' WHERE order_key=:k"), {"k": k})
        r = outbox.run_due(c, DryRunSender(), policy(), NOW)
        assert r[0]["status"] == "cancelled" and "human owns" in r[0]["reason"]


def test_T25_open_complaint_suppresses_promotions(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        c.execute(text("""INSERT INTO support_case(case_id, brand_id, contact_id, order_key, case_type, owner_team, priority, status, created_at, updated_at)
                          VALUES ('x', :b, 'c1', :k, 'complaint', 'cx', 'high', 'open', '2026-10-12', '2026-10-12')"""), {"b": BRAND, "k": k})
        r = outbox.run_due(c, DryRunSender(), policy(), NOW)
        assert r[0]["status"] == "cancelled" and "sensitive case" in r[0]["reason"]


def test_T38_frequency_cap_applies_across_channels(engine):
    with engine.begin() as c:
        k = seed(c)
        # a promotional touch already went out on EMAIL 2 hours ago
        c.execute(text("INSERT INTO touch_log(touch_id,brand_id,contact_id,category,channel_type,sent_at) VALUES ('t1',:b,'c1','promotional','email',:t)"),
                  {"b": BRAND, "t": (NOW - timedelta(hours=2)).isoformat()})
        promo(c, k)
        r = outbox.run_due(c, DryRunSender(), policy(), NOW)
        assert r[0]["status"] == "cancelled" and "24h promotional cap" in r[0]["reason"]


def test_T27_outside_window_defers_rather_than_sends(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        late = datetime(2026, 10, 12, 17, 30, tzinfo=timezone.utc)  # 23:00 IST
        r = outbox.run_due(c, DryRunSender(), policy(), late)
        assert r[0]["status"] == "queued" and r[0]["reason"] == "outside promotional window"


def test_T40_budget_guard_kill_switch_and_reserve(engine):
    with engine.begin() as c:
        k = seed(c, topup=250000); promo(c, k)
        r = outbox.run_due(c, DryRunSender(), policy(kill_switch=True), NOW)
        assert "kill switch" in r[0]["reason"]
    with engine.begin() as c:
        c.execute(text("DELETE FROM outbox_job"))
        promo(c, "amrit_dairy:shopify:order:1")
        # balance 2500.00 INR, reserve 2000.00, est cost 1.00 -> ok; reserve 2500.00 -> blocked
        r = outbox.run_due(c, DryRunSender(), policy(reserve_minor=250000), NOW)
        assert "operational reserve" in r[0]["reason"]
        # service message with exhausted wallet is HELD (deferred), not dropped
        outbox.enqueue(c, BRAND, "svc-1", "send_message", "service", {"channel_type": "whatsapp", "est_cost_minor": 300000}, NOW.isoformat(), "c1", k)
        consent.record_consent(c, BRAND, "c1", "whatsapp", "service", "granted", "x")
        r = outbox.run_due(c, DryRunSender(), policy(), NOW)
        assert r[-1]["status"] == "queued" and "escalate" in r[-1]["reason"]


def test_T19_ambiguous_timeout_requires_reconciliation_not_retry(engine):
    with engine.begin() as c:
        k = seed(c); promo(c, k)
        inner = DryRunSender(); s = FlakySender(inner)
        r = outbox.run_due(c, s, policy(), NOW)
        assert r[0]["status"] == "reconcile_required"
        r2 = outbox.run_due(c, s, policy(), NOW + timedelta(minutes=5))
        assert r2 == [] and s.calls == 1          # no blind retry
        assert outbox.reconcile(c, "amrit_dairy:shopify:order:1:abandoned_reminder_1", "bs_msg_123") == "done (provider had executed)"
        assert c.execute(text("SELECT COUNT(*) FROM action_ledger")).scalar() == 1


def test_T41_retry_exhaustion_becomes_dead_letter_task(engine):
    class Failing:
        def send(self, action_key, payload):
            return SendResult("failed", None, 0, "provider 500")
    with engine.begin() as c:
        k = seed(c)
        outbox.enqueue(c, BRAND, "svc-fail", "send_message", "service", {"channel_type": "whatsapp"}, NOW.isoformat(), "c1", k, max_attempts=2)
        consent.record_consent(c, BRAND, "c1", "whatsapp", "service", "granted", "x")
        t = NOW
        statuses = []
        for _ in range(3):
            res = outbox.run_due(c, Failing(), policy(), t)
            statuses += [r["status"] for r in res]
            t += timedelta(hours=3)
        assert "dead_letter" in statuses
        assert c.execute(text("SELECT COUNT(*) FROM support_case WHERE owner_team='technical'")).scalar() == 1


def test_T42_restart_does_not_burst_expired_jobs(engine):
    with engine.begin() as c:
        k = seed(c)
        outbox.enqueue(c, BRAND, "old-promo", "send_message", "promotional", {"purpose": "abandoned_checkout", "channel_type": "whatsapp"},
                       (NOW - timedelta(days=3)).isoformat(), "c1", k, (NOW - timedelta(days=2)).isoformat())
        r = outbox.run_due(c, DryRunSender(), policy(), NOW)   # simulated restart after outage
        assert r[0]["status"] == "cancelled" and r[0]["reason"] == "expired"
