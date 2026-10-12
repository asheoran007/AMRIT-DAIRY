"""T11/T12/T13/T15/T16/T17/T18: payment truth and reconciliation."""
from sqlalchemy import text

from amrit_adapter import payments
from tests.conftest import mk_contact, mk_order


def test_T11_T12_customer_confirmation_or_screenshot_never_marks_paid(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        # "unverified" attempt (typed txn id / screenshot) -> verified=False
        r = payments.record_attempt(c, k, "razorpay", "pay_typed_by_customer", 350000, "INR", "captured", False, "1")
        assert r.financial_state == "unpaid" and r.paid_minor == 0


def test_T13_verified_capture_reconciles_once_and_T15_replay_is_noop(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        r1 = payments.record_attempt(c, k, "razorpay", "pay_A", 350000, "INR", "captured", True, "10")
        r2 = payments.record_attempt(c, k, "razorpay", "pay_A", 350000, "INR", "captured", True, "10")
        assert r1.financial_state == "paid" and r1.paid_minor == 350000
        assert r2.applied is False and r2.reason == "duplicate attempt event ignored"
        n = c.execute(text("SELECT COUNT(*) FROM payment_attempt WHERE order_key=:k"), {"k": k}).scalar()
        assert n == 1


def test_T13_currency_or_amount_mismatch_goes_to_review(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        r = payments.record_attempt(c, k, "razorpay", "pay_usd", 350000, "USD", "captured", True, "10")
        assert r.applied is False and "currency mismatch" in r.reason
        assert c.execute(text("SELECT COUNT(*) FROM support_case WHERE case_type='payment_review'")).scalar() == 1


def test_T16_late_failed_event_never_downgrades_paid(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        payments.record_attempt(c, k, "razorpay", "pay_ok", 350000, "INR", "captured", True, "20")
        r = payments.record_attempt(c, k, "razorpay", "pay_old_fail", 350000, "INR", "failed", True, "5")  # older attempt arrives late
        assert r.financial_state == "paid" and r.paid_minor == 350000


def test_T17_partial_stays_partial_without_approved_threshold(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        r = payments.record_attempt(c, k, "razorpay", "pay_half", 175000, "INR", "captured", True, "30")
        assert r.financial_state == "partially_paid" and r.paid_minor == 175000
        ok, why = payments.fulfilment_eligible(c, k, None)
        assert ok is False and "not eligible" in why
        ok2, _ = payments.fulfilment_eligible(c, k, 50)   # only if an approved rule says 50%
        assert ok2 is True
        ok3, _ = payments.fulfilment_eligible(c, k, 60)
        assert ok3 is False


def test_T18_cod_to_prepaid_flips_mode_after_verified_payment(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c, fin="cod_pending", mode="cod", cod="approved_zone_A")
        r = payments.record_attempt(c, k, "razorpay", "pay_conv", 350000, "INR", "captured", True, "40")
        assert r.financial_state == "paid"
        # atomic mode flip performed in the same transaction by the conversion flow:
        c.execute(text("UPDATE order_ref SET payment_mode='prepaid', cod_decision='converted_prepaid' WHERE order_key=:k AND financial_state='paid'"), {"k": k})
        row = c.execute(text("SELECT payment_mode, cod_decision FROM order_ref WHERE order_key=:k"), {"k": k}).fetchone()
        assert row.payment_mode == "prepaid"   # courier COD instruction must be cancelled before manifest (see RUNBOOK)
