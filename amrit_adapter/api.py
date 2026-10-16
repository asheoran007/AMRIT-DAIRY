"""HTTP surface: provider webhooks (ack fast, process in worker) + health.

Run:  uvicorn amrit_adapter.api:app --host 127.0.0.1 --port 8787
Expose publicly only behind TLS (reverse proxy / tunnel) — see RUNBOOK.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from .config import settings
from .db import make_engine, migrate, tx
from .envelope import persist_event
from .webhooks import verify as v

log = logging.getLogger("amrit.api")

engine = make_engine(settings.adapter_database_url)
migrate(engine)
settings.load_stored_token(engine)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Single-service deployments (Render web service + 1 disk): run the worker loop in a daemon thread of this process."""
    if settings.adapter_embed_worker:
        import threading
        from . import worker as _w
        _w.setup_logging()
        threading.Thread(target=_w.run_forever, args=(engine, settings.adapter_worker_interval_s), name="embedded-worker", daemon=True).start()
    yield


app = FastAPI(title="amrit-adapter", version="0.2.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    with tx(engine) as conn:
        queued = conn.execute(text("SELECT COUNT(*) FROM outbox_job WHERE status='queued'")).scalar()
        dead = conn.execute(text("SELECT COUNT(*) FROM outbox_job WHERE status IN ('dead_letter','reconcile_required')")).scalar()
    return {"ok": True, "release": settings.release, "sender_mode": settings.sender_mode, "kill_switch": settings.global_kill_switch, "queued": queued, "needs_attention": dead,
            "embedded_worker": settings.adapter_embed_worker, "console_api": bool(settings.chatwoot_api_token), "console_auth": {k: v for k, v in console_auth_status().items() if k != "checked_at"},
            "bitespeed_api": bitespeed_api_status(),
            "shopify": settings.shopify_auth_mode, "shopify_webhooks": _worker_mod().webhook_status,
            "backup": _backup_status(), "inbox": inbox_stats}


def _backup_status() -> dict:
    from . import backup as _b
    return _b.status


def _worker_mod():
    from . import worker as _w
    return _w


@app.post("/webhooks/shopify")
async def shopify_webhook(request: Request) -> Response:
    raw = await request.body()
    try:
        v.verify_shopify(raw, request.headers, settings.effective_shopify_webhook_secret)
        ev = v.normalize_shopify(raw, request.headers, settings.adapter_brand_id)
    except v.WebhookRejected as exc:
        return Response(status_code=401, content=str(exc))
    shop = request.headers.get("X-Shopify-Shop-Domain", "")
    if settings.shopify_shop_domain and shop != settings.shopify_shop_domain:
        return Response(status_code=401, content="unexpected shop domain")  # cross-store leakage guard (T02)
    with tx(engine) as conn:
        persist_event(conn, ev)
    return Response(status_code=200)


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> Response:
    raw = await request.body()
    try:
        v.verify_razorpay(raw, request.headers, settings.razorpay_webhook_secret)
        ev = v.normalize_razorpay(raw, request.headers, settings.adapter_brand_id)
    except v.WebhookRejected as exc:
        return Response(status_code=401, content=str(exc))
    with tx(engine) as conn:
        persist_event(conn, ev)
    return Response(status_code=200)   # Razorpay requires 2xx within 5 s


def build_engine():
    """Conversation engine over the live catalogue + Shopify (live client when a token is configured, else the fake)."""
    from .engine.catalog import Catalog
    from .engine.shopify_client import FakeShopifyClient, LiveShopifyClient
    from .engine.state import Engine
    shop = LiveShopifyClient(settings.shopify_shop_domain, auth=settings.shopify_token_provider()) if settings.shopify_configured else FakeShopifyClient()
    return Engine(Catalog.load(), shop, settings.adapter_brand_id, _allowlist(), settings.test_payment_minor)


def _allowlist() -> tuple[str, ...]:
    return tuple(x.strip() for x in settings.test_phone_allowlist.split(",") if x.strip())


_console_check: dict = {"checked_at": None, "status": None, "agent": None, "accounts_ok": None}


def console_auth_status(force: bool = False) -> dict:
    """Read-only check of CHATWOOT_API_TOKEN: GET /api/v1/profile (sends nothing). Cached 10 min; shown on /health as
    console_auth so the owner can confirm the official token works before any live_test."""
    import time as _t
    import httpx as _x
    if not settings.chatwoot_api_token:
        _console_check.update(status="not_configured", agent=None, accounts_ok=None); return _console_check
    if not force and _console_check["checked_at"] and _t.time() - _console_check["checked_at"] < 600:
        return _console_check
    _console_check["checked_at"] = _t.time()
    try:
        r = _x.get(f"{settings.chatwoot_base_url.rstrip('/')}/api/v1/profile", headers={"api_access_token": settings.chatwoot_api_token}, timeout=15)
        if r.status_code == 200:
            p = r.json()
            ids = {int(x.get("id")) for x in (p.get("accounts") or []) if x.get("id") is not None}
            _console_check.update(status="ok", agent=p.get("name"), accounts_ok=settings.chatwoot_account_id in ids)
        else:
            _console_check.update(status=f"http_{r.status_code}", agent=None, accounts_ok=None)
    except Exception as exc:
        _console_check.update(status=f"error_{type(exc).__name__}", agent=None, accounts_ok=None)
    return _console_check


def bitespeed_api_status() -> dict:
    """Official BiteSpeed Messaging API readiness (doc received 16 Oct; DEPLOYMENT.md §4b-ii). Presence only - never the
    key. The API has no probe endpoint, so `verified` flips only after a real send succeeded (controlled live_test)."""
    from . import bitespeed_api as _b
    key, chan = bool(settings.bitespeed_api_key), bool(settings.bitespeed_channel_id)
    ready = settings.bitespeed_api_ready
    if not key:
        st = "not_configured"
    elif not chan:
        st = "key_present_missing_channel_id"
    elif _b.status["last_outcome"] == "queued":
        st = "ok"
    elif _b.status["last_outcome"]:
        st = f"last_send_{_b.status['last_outcome']}"
    else:
        st = "ready_unverified_no_probe_endpoint"
    return {"configured": key, "channel_id_set": chan, "status": st, "used_for_sending": ready, "verified": _b.status["last_outcome"] == "queued",
            "last_send": {k: _b.status[k] for k in ("last_send_at", "last_outcome", "last_http", "sent", "failed")}}


def _chatwoot():
    """Reply channel for inbound turns: official Messaging API when configured, else the console (Chatwoot) API, else None."""
    from .bitespeed_api import BitespeedReplyChannel
    from .conversation import ChatwootClient
    client = settings.bitespeed_client()
    if client is not None:
        return BitespeedReplyChannel(client)
    if not settings.chatwoot_api_token:
        return None
    return ChatwootClient(settings.chatwoot_base_url, settings.chatwoot_account_id, settings.chatwoot_api_token)


@app.post("/bot/turn/{token:path}")
async def bot_turn(token: str, request: Request) -> Response:
    """Called by a BiteSpeed flow 'API Call' node: {phone, text?, button_id?, name?} -> {state, messages[], actions[], handoff}.
    The flow renders messages[] (text/buttons/list/video) with its own Send/Quick-Reply/List nodes."""
    import hmac as _h
    import json as _j
    from .engine.state import render_plain
    if not settings.inbox_webhook_token or not _h.compare_digest(token, settings.inbox_webhook_token):
        return Response(status_code=401, content="bad token")
    body = _j.loads((await request.body()) or b"{}")
    import re as _re

    def _flat(d, pre=""):
        out = {}
        for k, val in (d or {}).items():
            if isinstance(val, dict):
                out.update(_flat(val, f"{pre}{k}."))
            else:
                out[f"{pre}{k}"] = val
        return out
    flat = _flat(body)
    def _ok(val):
        return isinstance(val, (str, int)) and str(val).strip() != "" and "{{" not in str(val)
    def _pick(*keys):
        for k in keys:
            if _ok(flat.get(k)):
                return str(flat[k]).strip()
        return ""
    phone = _pick("phone", "phone_number", "mobile", "customer_phone", "contact_phone", "whatsapp_number")
    if not phone:   # any filled value that looks like a phone number (BiteSpeed variable names are undocumented)
        phone = next((str(val).strip() for val in flat.values() if _ok(val) and _re.fullmatch(r"\+?\d{10,13}", str(val).strip())), "")
    text_in = _pick("text", "message", "last_message", "user_message", "message_text", "customer_message", "input")
    if not text_in:
        text_in = next((str(val).strip() for k, val in flat.items() if _ok(val) and str(val).strip() != phone
                        and not k.startswith(("name", "first", "last_name")) and "name" not in k.lower()), "")
    name = _pick("name", "customer_name", "first_name", "firstName") or None
    # Diagnostics without PII: per key -> empty / literal (unresolved {{x}}) / filled:<len>; header NAMES only.
    shape = {k: ("empty" if str(val).strip() == "" else "literal" if "{{" in str(val) else f"filled:{len(str(val))}") for k, val in flat.items()}
    log.info("bot.turn shape=%s headers=%s phone_last4=%s text_len=%d", shape,
             sorted(h for h in request.headers.keys() if h.lower() not in ("authorization", "cookie")), phone[-4:], len(text_in))
    if not phone:
        return Response(status_code=400, content="no phone")
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("0")
    from .conversation import deliver
    eng = build_engine()
    with tx(engine) as conn:
        r = eng.turn(conn, phone, text_in, body.get("button_id"), name)
        # 16 Oct finding: the console webhook never carries incoming customer messages while the BiteSpeed bot owns the
        # conversation, so this flow-builder entry is the inbound path - and the adapter delivers the answer itself over
        # the official Messaging API under the same guards (kill switch / SENDER_MODE / allowlist). Fire-and-forget safe.
        out = deliver(conn, phone, body.get("conversation_id") or f"flow:{phone[-4:]}", r, render_plain(r.messages), "",
                      _chatwoot(), settings.sender_mode, _allowlist(), settings.global_kill_switch, source="flow")
    return Response(status_code=200, media_type="application/json",
                    content=_j.dumps({"state": r.state, "messages": [m.as_dict() for m in r.messages], "actions": r.actions,
                                      "reply_text": render_plain(r.messages),            # for a single Text node: {{bot_reply}}
                                      "labels": [x.split(":", 1)[1] for x in r.actions if x.startswith("label:")],
                                      "handoff": r.handoff, "handoff_team": r.handoff_team,
                                      "delivery": {"status": out.status, "reason": out.reason}}, ensure_ascii=False))


def _public_base(request: Request) -> str:
    """Base URL for OAuth redirects: the configured public URL, or the request host when it is this service's Render
    hostname (lets the install run before the custom domain's DNS is live). Anything else falls back to the configured URL."""
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    cfg = settings.adapter_public_url.rstrip("/")
    if host and (host.endswith(".onrender.com") or (cfg and host == cfg.split("://", 1)[-1])):
        return f"https://{host}"
    return cfg


@app.get("/shopify/auth/start")
def shopify_auth_start(request: Request, token: str = "") -> Response:
    """Begins the authorization-code grant (custom distribution). Accepted entries:
    (a) owner link  ?token=<INBOX_WEBHOOK_TOKEN>  (or the masked form at GET/POST /shopify/auth - token never in a URL);
    (b) Shopify app-URL load: Shopify opens the configured App URL with ?shop&hmac&timestamp(&host) signed with the
        client secret (same scheme as the OAuth callback) - the documented install entry point, no shared secret in any link."""
    return _start_grant(request, token, {k: v for k, v in request.query_params.items()})


@app.get("/shopify/auth")
def shopify_auth_form() -> Response:
    """Owner install page: a masked field for the inbox token (paste from Render 'Copy value'), POSTed - never in a URL,
    never readable from a screenshot. Submitting runs the same token check as /shopify/auth/start."""
    html = ("<!doctype html><meta name=viewport content='width=device-width'><title>Amrit adapter - install Shopify app</title>"
            "<body style='font-family:system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem'>"
            "<h2>Install Amrit adapter on Shopify</h2><p>Paste the <b>INBOX_WEBHOOK_TOKEN</b> (Render &rarr; Environment &rarr; Copy value). "
            "It is sent once, masked, and only used to start Shopify's own approval screen.</p>"
            "<form method=post action='/shopify/auth/start'><input type=password name=token autocomplete=off required "
            "style='width:100%;padding:.6rem;font-size:1rem' placeholder='paste token'> "
            "<button style='margin-top:1rem;padding:.6rem 1.2rem;font-size:1rem'>Continue to Shopify approval</button></form></body>")
    return Response(content=html, media_type="text/html")


@app.post("/shopify/auth/start")
async def shopify_auth_start_post(request: Request) -> Response:
    from urllib.parse import parse_qs
    body = (await request.body())[:4096].decode("utf-8", "ignore")   # application/x-www-form-urlencoded, no multipart dep
    return _start_grant(request, parse_qs(body).get("token", [""])[0].strip(), {})


def _start_grant(request: Request, token: str, params: dict[str, str]) -> Response:
    import hmac as _h
    from fastapi.responses import RedirectResponse
    from .engine import shopify_oauth as so
    owner_ok = bool(settings.inbox_webhook_token) and bool(token) and _h.compare_digest(token, settings.inbox_webhook_token)
    shopify_ok = (params.get("shop") == settings.shopify_shop_domain and bool(settings.shopify_client_secret)
                  and so.verify_callback_hmac({k: v for k, v in params.items() if k != "token"}, settings.shopify_client_secret)
                  and so.fresh_timestamp(params.get("timestamp", "")))
    if not (owner_ok or shopify_ok):
        return Response(status_code=401, content="bad token")
    base = _public_base(request)
    if not (settings.shopify_client_id and settings.shopify_shop_domain and base):
        return Response(status_code=500, content="SHOPIFY_CLIENT_ID / SHOPIFY_SHOP_DOMAIN / ADAPTER_PUBLIC_URL not configured")
    state = so.make_state(settings.inbox_webhook_token, settings.shopify_shop_domain)
    return RedirectResponse(so.authorize_url(settings.shopify_shop_domain, settings.shopify_client_id,
                                             base + "/shopify/auth/callback", state), status_code=302)


@app.get("/shopify/auth/callback")
def shopify_auth_callback(request: Request) -> Response:
    """Shopify redirects here after the owner approves the scopes. Verifies hmac + state, exchanges the code, stores the offline token."""
    from .engine import shopify_oauth as so
    params = {k: v for k, v in request.query_params.items()}
    shop = params.get("shop", "")
    if shop != settings.shopify_shop_domain:
        return Response(status_code=400, content="unexpected shop")
    if not so.verify_callback_hmac(params, settings.shopify_client_secret):
        return Response(status_code=401, content="bad hmac")
    if not so.check_state(settings.inbox_webhook_token, shop, params.get("state", "")):
        return Response(status_code=401, content="bad state")
    try:
        body = so.exchange_code(shop, settings.shopify_client_id, settings.shopify_client_secret, params.get("code", ""))
    except Exception as exc:
        return Response(status_code=502, content=f"token exchange failed: {type(exc).__name__}")
    with tx(engine) as conn:
        so.store_token(conn, shop, body["access_token"], body.get("scope"))
    settings.load_stored_token(engine)
    # Register this app's webhook subscriptions right away (idempotent, self-healing every 30 min in the worker too).
    st = _worker_mod().ensure_shopify_webhooks(force=True) or {}
    base = settings.adapter_public_url.rstrip("/")
    wh_line = (f"webhooks -> {base}/webhooks/shopify: {st.get('subscribed')}/7 subscribed ({st.get('created')} created now)"
               + (f", errors: {st['errors']}" if st.get("errors") else ""))
    log.info("shopify install ok shop=%s %s", shop, wh_line)
    return Response(status_code=200, media_type="text/plain",
                    content=f"Amrit adapter installed on {shop}. scopes={body.get('scope')}.\n{wh_line}\nYou can close this tab.")


inbox_stats: dict = {"accepted": 0, "rejected": 0, "duplicates": 0, "last_accepted": None, "last_rejected_at": None}


@app.post("/webhooks/inbox/{token:path}")   # Render-generated tokens may contain "/" and "="
async def inbox_webhook(token: str, request: Request) -> Response:
    """BiteSpeed support console (account 3122) -> Settings -> Integrations -> Webhooks.
    URL to register: https://<host>/webhooks/inbox/<INBOX_WEBHOOK_TOKEN>. Hint-level auth only."""
    from .db import now_iso as _now
    raw = await request.body()
    try:
        level = v.verify_inbox_webhook(token, settings.inbox_webhook_token)
        ev = v.normalize_inbox_webhook(raw, settings.adapter_brand_id, level)
    except v.WebhookRejected as exc:
        inbox_stats["rejected"] += 1; inbox_stats["last_rejected_at"] = _now()
        return Response(status_code=401, content=str(exc))
    import json as _j
    from .conversation import handle_inbound
    conv = (ev.payload.get("conversation") or {}).get("id") or ev.payload.get("conversation_id")
    with tx(engine) as conn:
        res = persist_event(conn, ev)
        if res.duplicate:
            inbox_stats["duplicates"] += 1
            return Response(status_code=200, content="duplicate")
        inbox_stats["accepted"] += 1
        summary = {"event": ev.event_type.split(".", 1)[1], "conversation_id": conv, "event_id": res.event_id, "received_at": ev.received_at, "result": None}
        if ev.event_type == "bitespeed_inbox.message_created":
            try:
                out = handle_inbound(conn, ev.payload, build_engine, _chatwoot(), settings.sender_mode, _allowlist(),
                                     settings.global_kill_switch,
                                     tuple(k.strip() for k in settings.flow_keywords_yield.split(",") if k.strip()))
            except Exception:
                log.exception("inbound message handling failed (event stays persisted)")
                summary["result"] = "error-logged"; inbox_stats["last_accepted"] = summary
                return Response(status_code=200, content="error-logged")
            summary["result"] = f"{out.status}: {out.reason}"; inbox_stats["last_accepted"] = summary
            return Response(status_code=200, media_type="application/json",
                            content=_j.dumps({"status": out.status, "reason": out.reason, "state": out.state}))
        if ev.event_type in ("bitespeed_inbox.conversation_updated", "bitespeed_inbox.conversation_status_changed",
                             "bitespeed_inbox.conversation_created"):
            from .conversation import incoming_from_conversation_event
            from .envelope import InternalEvent as _IE
            p = ev.payload
            log.info("inbox %s conv=%s shape msgs=%d types=%s has_last=%s sender_last4=%s", ev.event_type.split(".", 1)[1], conv,
                     len(p.get("messages") or []), [m.get("message_type") for m in (p.get("messages") or []) if isinstance(m, dict)][:5],
                     isinstance(p.get("last_non_activity_message"), dict),
                     str((((p.get("meta") or {}).get("sender")) or {}).get("phone_number") or "")[-4:])
            results = []
            for msg in incoming_from_conversation_event(p):
                dedupe = _IE(brand_id=ev.brand_id, source="bitespeed_inbox_msg", source_event_id=f"msg:{msg['id']}",
                             source_object_id=str(msg["id"]), event_type="bitespeed_inbox.message_derived",
                             occurred_at=str(msg.get("created_at") or ""), auth_level=ev.auth_level, payload={"conversation_id": conv})
                if persist_event(conn, dedupe).duplicate:
                    continue
                try:
                    out = handle_inbound(conn, msg, build_engine, _chatwoot(), settings.sender_mode, _allowlist(),
                                         settings.global_kill_switch,
                                         tuple(k.strip() for k in settings.flow_keywords_yield.split(",") if k.strip()))
                    results.append(f"{out.status}: {out.reason}")
                except Exception:
                    log.exception("derived inbound handling failed (event stays persisted)")
                    results.append("error-logged")
            if results:
                summary["event"] = "message_derived"; summary["result"] = "; ".join(results)
        inbox_stats["last_accepted"] = summary
    return Response(status_code=200)


@app.post("/webhooks/shiprocket")
async def shiprocket_webhook(request: Request) -> Response:
    raw = await request.body()
    try:
        level = v.verify_shiprocket(request.headers, settings.shiprocket_webhook_token)
        ev = v.normalize_shiprocket(raw, request.headers, settings.adapter_brand_id, level)
    except v.WebhookRejected as exc:
        return Response(status_code=401, content=str(exc))
    with tx(engine) as conn:
        persist_event(conn, ev)
    return Response(status_code=200)
