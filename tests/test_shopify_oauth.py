"""Authorization-code grant for the merchant store (not a dev store): state + hmac verification, token storage, provider pickup."""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from amrit_adapter.engine import shopify_oauth as so


def _sign(params, secret):
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def test_state_roundtrip_and_expiry():
    st = so.make_state("sec", "kmaz1y-dq.myshopify.com")
    assert so.check_state("sec", "kmaz1y-dq.myshopify.com", st)
    assert not so.check_state("other", "kmaz1y-dq.myshopify.com", st)
    old = so.make_state("sec", "kmaz1y-dq.myshopify.com", ts=int(time.time()) - 3600)
    assert not so.check_state("sec", "kmaz1y-dq.myshopify.com", old)


def test_callback_hmac_verification():
    p = {"code": "abc", "shop": "kmaz1y-dq.myshopify.com", "state": "x", "timestamp": "1"}
    p["hmac"] = _sign(p, "cs")
    assert so.verify_callback_hmac(p, "cs") and not so.verify_callback_hmac(p, "wrong")


def test_full_install_flow_stores_offline_token_and_provider_uses_it(monkeypatch, tmp_path):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "shopify_shop_domain", "kmaz1y-dq.myshopify.com")
    monkeypatch.setattr(settings, "shopify_client_id", "cid"); monkeypatch.setattr(settings, "shopify_client_secret", "cs")
    monkeypatch.setattr(settings, "adapter_public_url", "https://adapter.amritdairy.in"); monkeypatch.setattr(settings, "_stored_token", None)
    c = TestClient(api.app)
    assert c.get("/shopify/auth/start?token=bad").status_code == 401
    r = c.get("/shopify/auth/start?token=t0k", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://kmaz1y-dq.myshopify.com/admin/oauth/authorize?client_id=cid&scope=read_orders")
    state = r.headers["location"].split("state=")[1]
    monkeypatch.setattr(so.httpx, "post", lambda url, json=None, timeout=None: type("R", (), {"status_code": 200, "json": lambda self: {"access_token": "shpat_offline", "scope": so.SCOPES}})())
    p = {"code": "c0de", "shop": "kmaz1y-dq.myshopify.com", "state": state, "timestamp": str(int(time.time()))}
    bad = dict(p, hmac=_sign(p, "wrong")); assert c.get("/shopify/auth/callback", params=bad).status_code == 401
    p["hmac"] = _sign(p, "cs")
    r = c.get("/shopify/auth/callback", params=p)
    assert r.status_code == 200 and "installed" in r.text
    assert settings._stored_token == "shpat_offline" and settings.shopify_auth_mode == "offline_token"
    assert settings.shopify_token_provider().token() == "shpat_offline"       # no client-credentials call needed
    assert c.get("/health").json()["shopify"] == "offline_token"


def test_start_uses_render_host_before_custom_domain_dns(monkeypatch):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "shopify_shop_domain", "kmaz1y-dq.myshopify.com")
    monkeypatch.setattr(settings, "shopify_client_id", "cid"); monkeypatch.setattr(settings, "adapter_public_url", "https://adapter.amritdairy.in")
    c = TestClient(api.app)
    r = c.get("/shopify/auth/start?token=t0k", headers={"host": "amrit-adapter.onrender.com"}, follow_redirects=False)
    assert "redirect_uri=https%3A%2F%2Famrit-adapter.onrender.com%2Fshopify%2Fauth%2Fcallback" in r.headers["location"]
    r = c.get("/shopify/auth/start?token=t0k", headers={"host": "evil.example"}, follow_redirects=False)
    assert "redirect_uri=https%3A%2F%2Fadapter.amritdairy.in%2Fshopify%2Fauth%2Fcallback" in r.headers["location"]


def test_start_accepts_shopify_signed_app_url_load_but_not_forgeries(monkeypatch):
    """Shopify opens the App URL with ?shop&hmac&timestamp&host signed with the client secret: that starts OAuth too."""
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "shopify_shop_domain", "kmaz1y-dq.myshopify.com")
    monkeypatch.setattr(settings, "shopify_client_id", "cid"); monkeypatch.setattr(settings, "shopify_client_secret", "cs")
    monkeypatch.setattr(settings, "adapter_public_url", "https://adapter.amritdairy.in")
    c = TestClient(api.app)
    p = {"shop": "kmaz1y-dq.myshopify.com", "timestamp": str(int(time.time())), "host": "YWRtaW4"}
    good = dict(p, hmac=_sign(p, "cs"))
    r = c.get("/shopify/auth/start", params=good, follow_redirects=False)
    assert r.status_code == 302 and "/admin/oauth/authorize?" in r.headers["location"]
    assert c.get("/shopify/auth/start", params=dict(good, hmac="0" * 64), follow_redirects=False).status_code == 401   # bad signature
    other = dict(p, shop="other.myshopify.com"); other["hmac"] = _sign(other, "cs")
    assert c.get("/shopify/auth/start", params=other, follow_redirects=False).status_code == 401                        # wrong shop
    stale = dict(p, timestamp=str(int(time.time()) - 3600)); stale["hmac"] = _sign(stale, "cs")
    assert c.get("/shopify/auth/start", params=stale, follow_redirects=False).status_code == 401                        # replay
    assert c.get("/shopify/auth/start", follow_redirects=False).status_code == 401                                      # nothing
