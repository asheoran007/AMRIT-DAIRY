"""Background worker: process inbox events, run due outbox jobs, generate exception report.

Run:  python -m amrit_adapter.worker --once      (one pass; use the OS scheduler for recurring runs)
      python -m amrit_adapter.worker --loop 30   (poll every 30 s)
Sender: DryRunSender unless SENDER_MODE=live|live_test AND CHATWOOT_API_TOKEN is set (see sender.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from sqlalchemy import text

from . import backup, handlers, outbox, reports
from .config import settings
from .db import make_engine, migrate, now_iso, tx
from .envelope import InternalEvent
from .sender import ChatwootSender, DryRunSender, GuardedSender


log = logging.getLogger("amrit.worker")


def setup_logging() -> None:
    """One line per event, no secrets, no customer PII beyond ids; stdout -> docker/journald."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def build_policy() -> outbox.Policy:
    return outbox.Policy(
        brand_id=settings.adapter_brand_id,
        promo_window_start=settings.promo_window_start,
        promo_window_end=settings.promo_window_end,
        promo_max_per_24h=settings.promo_max_per_24h,
        promo_max_per_7d=settings.promo_max_per_7d,
        reserve_minor=(settings.operational_reserve_inr or 0) * 100,
        monthly_cap_minor=(settings.budget_monthly_cap_inr * 100) if settings.budget_monthly_cap_inr else None,
        kill_switch=settings.global_kill_switch,
    )


def build_sender():
    """Outbox sender under SENDER_MODE. Without CHATWOOT_API_TOKEN every mode degrades to dry-run (nothing can be delivered)."""
    if settings.sender_mode in ("live", "live_test") and settings.chatwoot_api_token:
        inner = ChatwootSender(settings.chatwoot_base_url, settings.chatwoot_account_id, settings.chatwoot_api_token)
        return GuardedSender(inner, settings.sender_mode, tuple(x.strip() for x in settings.test_phone_allowlist.split(",") if x.strip()))
    return DryRunSender()


def build_shopify_client():
    """Live Admin client when the custom-app token is configured (needed for payment read-back); else None (engine link skipped)."""
    if settings.shopify_configured:
        from .engine.shopify_client import LiveShopifyClient
        return LiveShopifyClient(settings.shopify_shop_domain, auth=settings.shopify_token_provider())
    return None


def process_inbox(engine, limit: int = 200, shopify_client=None) -> int:
    n = 0
    with tx(engine) as conn:
        rows = conn.execute(text("SELECT * FROM event_inbox WHERE processed_at IS NULL ORDER BY received_at LIMIT :l"), {"l": limit}).fetchall()
        for r in rows:
            ev = InternalEvent(brand_id=r.brand_id, source=r.source, source_event_id=r.source_event_id, source_object_id=r.source_object_id,
                               event_type=r.event_type, occurred_at=r.occurred_at, received_at=r.received_at, schema_version=r.schema_version,
                               correlation_id=r.correlation_id, auth_level=r.auth_level, payload=json.loads(r.payload_json))
            actions = handlers.handle(conn, ev, settings.abandoned_checkout_first_delay_min, shopify_client=shopify_client)
            conn.execute(text("UPDATE event_inbox SET processed_at=:t WHERE event_id=:e"), {"t": now_iso(), "e": r.event_id})
            n += 1
            log.info("inbox event_type=%s source_event_id=%s actions=%s", ev.event_type, ev.source_event_id, actions)
    return n


webhook_status: dict = {"checked_at": None, "subscribed": None, "created": None, "errors": None}
_next_webhook_check = 0.0


def ensure_shopify_webhooks(force: bool = False) -> dict | None:
    """Self-healing: every 30 min (and after install) make sure this app's 7 webhook subscriptions exist on the store.
    Only runs with an offline token (install done) and a public URL; result is exposed on /health as shopify_webhooks."""
    global _next_webhook_check
    if not force and time.time() < _next_webhook_check:
        return None
    _next_webhook_check = time.time() + 1800
    tok = settings.shopify_admin_token or settings._stored_token
    if not (tok and settings.shopify_shop_domain and settings.adapter_public_url):
        return None
    from .tools.register_shopify_webhooks import TOPICS, ensure_subscriptions
    try:
        res = ensure_subscriptions(settings.shopify_shop_domain, tok, settings.adapter_public_url)
        base = settings.adapter_public_url.rstrip("/")
        have = {t for t, u in res["existing"]} | {t for t, u in res["created"]}
        webhook_status.update(checked_at=now_iso(), subscribed=len(have & set(TOPICS)), created=len(res["created"]), errors=res["errors"] or None)
        log.info("shopify webhooks: subscribed=%s/%s created=%s errors=%s -> %s/webhooks/shopify", len(have & set(TOPICS)), len(TOPICS),
                 len(res["created"]), res["errors"], base)
    except Exception as exc:
        webhook_status.update(checked_at=now_iso(), errors=[f"{type(exc).__name__}: {exc}"[:200]])
        log.warning("shopify webhook check failed: %s", type(exc).__name__)
    return webhook_status


def one_pass(engine, sender, policy, shopify_client) -> None:
    process_inbox(engine, shopify_client=shopify_client)
    ensure_shopify_webhooks()
    backup.maybe_backup(settings.adapter_database_url, settings.backup_interval_s, settings.backup_keep)
    with tx(engine) as conn:
        for r in outbox.run_due(conn, sender, policy):
            log.info("outbox %s", r)
    if settings.reports_enabled:
        with tx(engine) as conn:
            log.info("report %s", reports.daily_exceptions(conn, settings.adapter_brand_id))


def run_forever(engine, interval_s: int) -> None:
    """Used by the embedded-worker mode (API process) and by --loop. Never dies on a bad event: logged, retried next pass."""
    sender = build_sender()
    policy = build_policy()
    shopify_client = build_shopify_client()
    log.info("worker loop started interval=%ss shopify=%s", interval_s, "live" if shopify_client else "none")
    while True:
        try:
            one_pass(engine, sender, policy, shopify_client)
        except Exception:
            log.exception("worker pass failed")
        time.sleep(interval_s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0)
    args = ap.parse_args()
    setup_logging()
    engine = make_engine(settings.adapter_database_url)
    migrate(engine)
    settings.load_stored_token(engine)
    if args.loop and not args.once:
        run_forever(engine, args.loop)
        return
    sender = build_sender()
    one_pass(engine, sender, build_policy(), build_shopify_client())


if __name__ == "__main__":
    main()
