"""Authorization-code grant (custom distribution) for a Dev Dashboard app on a store that is NOT a dev store of the org.

Why: the client-credentials grant works only when the store is listed under the app's organization in the Dev Dashboard
("owning or installing a store doesn't put it in your org"). Amrit Dairy's production store kmaz1y-dq is a merchant store,
so the documented path is: custom distribution + authorization code grant -> a non-expiring OFFLINE token
(valid until the app is uninstalled). Sources read 12 Oct 2026:
https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant (same-organization rule)
https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant

Flow:  GET /shopify/auth/start?token=<INBOX_WEBHOOK_TOKEN>  -> 302 https://{shop}/admin/oauth/authorize?...   (owner approves scopes)
       GET /shopify/auth/callback?code&hmac&shop&state&timestamp -> verify hmac + state -> POST /admin/oauth/access_token
       -> store token in table shopify_token -> TokenProvider(static) uses it. Secrets never leave the server.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

SCOPES = "read_orders,read_customers,read_products,read_draft_orders,write_draft_orders,read_checkouts,read_fulfillments"


def make_state(secret: str, shop: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{shop}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def check_state(secret: str, shop: str, state: str, max_age_s: int = 900) -> bool:
    try:
        ts_s, sig = state.split(".", 1)
        ts = int(ts_s)
    except ValueError:
        return False
    if abs(time.time() - ts) > max_age_s:
        return False
    return hmac.compare_digest(make_state(secret, shop, ts).split(".", 1)[1], sig)


def authorize_url(shop_domain: str, client_id: str, redirect_uri: str, state: str) -> str:
    q = {"client_id": client_id, "scope": SCOPES, "redirect_uri": redirect_uri, "state": state}
    return f"https://{shop_domain}/admin/oauth/authorize?{urlencode(q)}"


def verify_callback_hmac(params: dict[str, str], client_secret: str) -> bool:
    """Shopify signs the callback query: HMAC-SHA256 (hex) over the sorted, URL-decoded params except hmac."""
    given = params.get("hmac", "")
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    calc = hmac.new(client_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(calc, given)


def fresh_timestamp(ts: str, max_age_s: int = 900) -> bool:
    """Shopify-signed app-URL loads carry a unix `timestamp`; reject stale/replayed ones."""
    try:
        return abs(time.time() - int(ts)) <= max_age_s
    except ValueError:
        return False


def exchange_code(shop_domain: str, client_id: str, client_secret: str, code: str) -> dict:
    r = httpx.post(f"https://{shop_domain}/admin/oauth/access_token",
                   json={"client_id": client_id, "client_secret": client_secret, "code": code}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed: HTTP {r.status_code}")
    body = r.json()
    if "access_token" not in body:
        raise RuntimeError("token exchange: no access_token in response")
    return body


def store_token(conn, shop_domain: str, access_token: str, scope: str | None) -> None:
    from ..db import now_iso
    conn.execute(text("DELETE FROM shopify_token WHERE shop_domain=:s"), {"s": shop_domain})
    conn.execute(text("INSERT INTO shopify_token(shop_domain, access_token, scope, obtained_at) VALUES (:s,:t,:sc,:n)"),
                 {"s": shop_domain, "t": access_token, "sc": scope, "n": now_iso()})


def load_token(conn, shop_domain: str) -> str | None:
    row = conn.execute(text("SELECT access_token FROM shopify_token WHERE shop_domain=:s"), {"s": shop_domain}).fetchone()
    return row[0] if row else None
