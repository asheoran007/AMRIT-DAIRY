"""Dev Dashboard client-credentials auth (24 h tokens, renewal), 401/429 recovery, compose config sanity. MOCK: no network."""
import time

import httpx
import pytest

from amrit_adapter.engine import shopify_auth
from amrit_adapter.engine.shopify_auth import ShopifyAuthError, TokenProvider
from amrit_adapter.engine.shopify_client import LiveShopifyClient


class _Resp:
    def __init__(self, status, body=None, text="", headers=None):
        self.status_code, self._body, self.text, self.headers = status, body or {}, text, headers or {}
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400: raise httpx.HTTPStatusError("err", request=None, response=None)


def test_client_credentials_token_is_cached_and_renewed_before_expiry(monkeypatch):
    calls = []
    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        calls.append((url, data["grant_type"], data["client_id"]))
        return _Resp(200, {"access_token": f"tok{len(calls)}", "scope": "read_orders", "expires_in": 86399})
    monkeypatch.setattr(shopify_auth.httpx, "post", fake_post)
    tp = TokenProvider("kmaz1y-dq.myshopify.com", client_id="cid", client_secret="sec")
    assert tp.mode == "client_credentials"
    assert tp.token() == "tok1" and tp.token() == "tok1" and len(calls) == 1            # cached
    assert calls[0] == ("https://kmaz1y-dq.myshopify.com/admin/oauth/access_token", "client_credentials", "cid")
    tp._expires_at = time.time() + 30                                                    # inside the 60 s renewal margin
    assert tp.token() == "tok2" and len(calls) == 2                                      # renewed
    assert tp.token(force_refresh=True) == "tok3"


def test_static_token_never_calls_the_grant(monkeypatch):
    monkeypatch.setattr(shopify_auth.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    tp = TokenProvider("kmaz1y-dq.myshopify.com", static_token="shpat_x")
    assert tp.mode == "static" and tp.token() == "shpat_x" and tp.seconds_left() is None


def test_missing_credentials_or_shop_not_permitted_fail_loudly(monkeypatch):
    with pytest.raises(ShopifyAuthError):
        TokenProvider("kmaz1y-dq.myshopify.com")
    monkeypatch.setattr(shopify_auth.httpx, "post", lambda *a, **k: _Resp(400, text='{"error":"shop_not_permitted"}'))
    with pytest.raises(ShopifyAuthError, match="shop_not_permitted"):
        TokenProvider("kmaz1y-dq.myshopify.com", client_id="cid", client_secret="sec").token()


def test_live_client_renews_on_401_and_backs_off_on_429(monkeypatch):
    import httpx as _httpx
    tokens = iter(["tokA", "tokB"]); grants = []; seen = []
    def fake_post(url, data=None, headers=None, json=None, timeout=None, **kw):   # httpx.post is shared by both modules
        if url.endswith("/admin/oauth/access_token"):
            grants.append(1); return _Resp(200, {"access_token": next(tokens), "expires_in": 86399})
        seen.append(headers["X-Shopify-Access-Token"])
        if len(seen) == 1: return _Resp(401, text="unauthorized")
        if len(seen) == 2: return _Resp(429, headers={"Retry-After": "0"})
        return _Resp(200, {"data": {"shop": {"name": "Amrit Dairy"}}})
    monkeypatch.setattr(_httpx, "post", fake_post)
    c = LiveShopifyClient("kmaz1y-dq.myshopify.com", auth=TokenProvider("kmaz1y-dq.myshopify.com", client_id="cid", client_secret="sec"))
    assert c._gql("query{shop{name}}", {}) == {"shop": {"name": "Amrit Dairy"}}
    assert seen == ["tokA", "tokB", "tokB"] and len(grants) == 2        # 401 -> forced renewal; 429 -> retry with same token


def test_settings_prefers_client_secret_for_webhooks_when_explicit_secret_blank(monkeypatch):
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "shopify_webhook_secret", ""); monkeypatch.setattr(settings, "shopify_client_secret", "cs")
    assert settings.effective_shopify_webhook_secret == "cs"
    monkeypatch.setattr(settings, "shopify_webhook_secret", "explicit")
    assert settings.effective_shopify_webhook_secret == "explicit"


def test_compose_does_not_interpolate_adapter_host_from_env_file():
    """Compose ${VAR} interpolation ignores env_file -> ADAPTER_HOST must reach Caddy only via env_file."""
    from pathlib import Path
    txt = (Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.yml").read_text()
    assert "${ADAPTER_HOST" not in txt and ".env.caddy" in txt and "adapter-data:/data" in txt
