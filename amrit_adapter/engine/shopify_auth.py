"""Shopify Admin API credentials for a Dev Dashboard app (2026 model).

Legacy admin-created custom apps were deprecated on 1 Jan 2026 (Shopify staff, community.shopify.dev #28387). A server-side
Dev Dashboard app in the SAME Shopify organization as the store obtains Admin API tokens with the **client credentials grant**:

    POST https://{shop}.myshopify.com/admin/oauth/access_token   (application/x-www-form-urlencoded)
    grant_type=client_credentials&client_id=...&client_secret=...
    -> {"access_token": "...", "scope": "...", "expires_in": 86399}

Tokens live 24 h; the official example re-requests one when within 60 s of expiry. Source (read 12 Oct 2026):
https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens

A static offline token (SHOPIFY_ADMIN_TOKEN, from the authorization-code grant or a still-valid legacy app) is also accepted.
Webhook HMACs for subscriptions created by the app are computed with the app's client secret.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("amrit.shopify.auth")
RENEW_MARGIN_S = 60


class ShopifyAuthError(RuntimeError):
    pass


@dataclass
class TokenProvider:
    shop_domain: str                     # kmaz1y-dq.myshopify.com
    static_token: str = ""               # SHOPIFY_ADMIN_TOKEN (non-expiring) — wins when set
    client_id: str = ""                  # Dev Dashboard app credentials (client credentials grant)
    client_secret: str = ""
    _token: str = ""
    _expires_at: float = 0.0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        if not self.shop_domain:
            raise ShopifyAuthError("SHOPIFY_SHOP_DOMAIN not configured")
        if not self.static_token and not (self.client_id and self.client_secret):
            raise ShopifyAuthError("configure SHOPIFY_ADMIN_TOKEN or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")

    @property
    def mode(self) -> str:
        return "static" if self.static_token else "client_credentials"

    def token(self, force_refresh: bool = False) -> str:
        if self.static_token:
            return self.static_token
        with self._lock:
            if force_refresh or not self._token or time.time() >= self._expires_at - RENEW_MARGIN_S:
                self._fetch()
            return self._token

    def _fetch(self) -> None:
        url = f"https://{self.shop_domain}/admin/oauth/access_token"
        r = httpx.post(url, data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
                       headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        if r.status_code != 200:
            # body may say shop_not_permitted (app and store in different organizations) — never log secrets
            raise ShopifyAuthError(f"client_credentials grant failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in") or 86399)
        log.info("shopify token renewed mode=client_credentials scope=%s expires_in=%s", body.get("scope"), body.get("expires_in"))

    def seconds_left(self) -> float | None:
        return None if self.static_token else max(0.0, self._expires_at - time.time())
