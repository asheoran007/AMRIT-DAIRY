"""HTTP surface: provider webhooks (ack fast, process in worker) + health.

Run:  uvicorn amrit_adapter.api:app --host 127.0.0.1 --port 8787
Expose publicly only behind TLS (reverse proxy / tunnel) — see RUNBOOK.md.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from .config import settings
from .db import make_engine, migrate, tx
from .envelope import persist_event
from .webhooks import verify as v

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
    return {"ok": True, "sender_mode": settings.sender_mode, "kill_switch": settings.global_kill_switch, "queued": queued, "needs_attention": dead,
            "embedded_worker": settings.adapter_embed_worker,
            "shopify": settings.shopify_auth_mode}


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


@app.post("/bot/turn/{token}")
async def bot_turn(token: str, request: Request) -> Response:
    """Called by a BiteSpeed flow 'API Call' node: {phone, text?, button_id?, name?} -> {state, messages[], actions[], handoff}.
    The flow renders messages[] (text/buttons/list/video) with its own Send/Quick-Reply/List nodes."""
    import hmac as _h
    import json as _j
    from .engine.catalog import Catalog
    from .engine.shopify_client import FakeShopifyClient, LiveShopifyClient
    from .engine.state import Engine, render_plain
    if not settings.inbox_webhook_token or not _h.compare_digest(token, settings.inbox_webhook_token):
        return Response(status_code=401, content="bad token")
    body = _j.loads((await request.body()) or b"{}")
    phone = str(body.get("phone") or "").strip()
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("0")
    cat = Catalog.load()
    shop = LiveShopifyClient(settings.shopify_shop_domain, auth=settings.shopify_token_provider()) if settings.shopify_configured else FakeShopifyClient()
    eng = Engine(cat, shop, settings.adapter_brand_id,
                 tuple(x.strip() for x in settings.test_phone_allowlist.split(",") if x.strip()), settings.test_payment_minor)
    with tx(engine) as conn:
        r = eng.turn(conn, phone, body.get("text") or "", body.get("button_id"), body.get("name"))
    return Response(status_code=200, media_type="application/json",
                    content=_j.dumps({"state": r.state, "messages": [m.as_dict() for m in r.messages], "actions": r.actions,
                                      "reply_text": render_plain(r.messages),            # for a single Text node: {{bot_reply}}
                                      "labels": [x.split(":", 1)[1] for x in r.actions if x.startswith("label:")],
                                      "handoff": r.handoff, "handoff_team": r.handoff_team}, ensure_ascii=False))


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
    """Begins the authorization-code grant (custom distribution). Two accepted entries:
    (a) owner link  ?token=<INBOX_WEBHOOK_TOKEN>;
    (b) Shopify app-URL load: Shopify opens the configured App URL with ?shop&hmac&timestamp(&host) signed with the
        client secret (same scheme as the OAuth callback) - the documented install entry point, no shared secret in any link."""
    import hmac as _h
    from fastapi.responses import RedirectResponse
    from .engine import shopify_oauth as so
    params = {k: v for k, v in request.query_params.items()}
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
    return Response(status_code=200, media_type="text/plain",
                    content=f"Amrit adapter installed on {shop}. scopes={body.get('scope')}. You can close this tab.")


@app.post("/webhooks/inbox/{token}")
async def inbox_webhook(token: str, request: Request) -> Response:
    """BiteSpeed support console (account 3122) -> Settings -> Integrations -> Webhooks.
    URL to register: https://<host>/webhooks/inbox/<INBOX_WEBHOOK_TOKEN>. Hint-level auth only."""
    raw = await request.body()
    try:
        level = v.verify_inbox_webhook(token, settings.inbox_webhook_token)
        ev = v.normalize_inbox_webhook(raw, settings.adapter_brand_id, level)
    except v.WebhookRejected as exc:
        return Response(status_code=401, content=str(exc))
    with tx(engine) as conn:
        persist_event(conn, ev)
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
