"""T13/T14/T15/T02: signature verification, rejection, inbox idempotency, cross-store guard."""
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from amrit_adapter.envelope import persist_event
from amrit_adapter.webhooks import verify as v

SECRET = "test-secret"


def shopify_headers(body: bytes, topic="checkouts/update", event_id="evt-1", secret=SECRET, shop="kmaz1y-dq.myshopify.com"):
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return {"X-Shopify-Hmac-SHA256": sig, "X-Shopify-Topic": topic, "X-Shopify-Event-Id": event_id,
            "X-Shopify-Webhook-Id": "wh-1", "X-Shopify-Shop-Domain": shop}


def test_T13_shopify_valid_signature_normalizes():
    body = json.dumps({"id": 123, "updated_at": "2026-10-11T10:00:00Z"}).encode()
    v.verify_shopify(body, shopify_headers(body), SECRET)
    ev = v.normalize_shopify(body, shopify_headers(body), "amrit_dairy")
    assert ev.event_type == "shopify.checkouts.update" and ev.auth_level == "verified_signature"
    assert ev.source_event_id == "checkouts/update:evt-1"


def test_T14_shopify_invalid_signature_rejected():
    body = b'{"id":1}'
    with pytest.raises(v.WebhookRejected):
        v.verify_shopify(body, shopify_headers(body, secret="wrong"), SECRET)
    with pytest.raises(v.WebhookRejected):
        v.verify_shopify(body, shopify_headers(body), "")  # unconfigured secret never passes


def test_T13_razorpay_signature_and_event_id():
    body = json.dumps({"event": "payment_link.paid", "payload": {"payment": {"entity": {"id": "pay_1", "created_at": 1}}}}).encode()
    sig = hmac.new(b"rzp", body, hashlib.sha256).hexdigest()
    h = {"X-Razorpay-Signature": sig, "x-razorpay-event-id": "rzp-evt-1"}
    v.verify_razorpay(body, h, "rzp")
    ev = v.normalize_razorpay(body, h, "amrit_dairy")
    assert ev.source_event_id == "rzp-evt-1" and ev.event_type == "razorpay.payment_link.paid"
    with pytest.raises(v.WebhookRejected):
        v.verify_razorpay(body + b" ", h, "rzp")  # raw body must match byte-for-byte


def test_T14_shiprocket_static_token_is_only_a_hint():
    assert v.verify_shiprocket({"x-api-key": "tok"}, "tok") == "static_token_hint"
    assert v.verify_shiprocket({}, "") == "unauthenticated"
    with pytest.raises(v.WebhookRejected):
        v.verify_shiprocket({"x-api-key": "bad"}, "tok")


def test_T15_inbox_duplicate_event_stored_once(engine):
    body = b'{"id": 5}'
    ev = v.normalize_shopify(body, shopify_headers(body, event_id="dup-1"), "amrit_dairy")
    with engine.begin() as c:
        r1 = persist_event(c, ev)
        r2 = persist_event(c, ev)
    assert not r1.duplicate and r2.duplicate and r1.event_id == r2.event_id


def test_T02_cross_store_delivery_rejected(monkeypatch, tmp_path):
    from amrit_adapter import api
    monkeypatch.setattr(api.settings, "shopify_webhook_secret", SECRET)
    monkeypatch.setattr(api.settings, "shopify_shop_domain", "kmaz1y-dq.myshopify.com")
    client = TestClient(api.app)
    body = json.dumps({"id": 77}).encode()
    bad = client.post("/webhooks/shopify", content=body, headers=shopify_headers(body, event_id="x-1", shop="theartshoes.myshopify.com"))
    assert bad.status_code == 401
    good = client.post("/webhooks/shopify", content=body, headers=shopify_headers(body, event_id="x-2"))
    assert good.status_code == 200


def test_inbox_webhook_token_and_dedupe(monkeypatch):
    from amrit_adapter import api
    monkeypatch.setattr(api.settings, "inbox_webhook_token", "tok123")
    client = TestClient(api.app)
    body = json.dumps({"event": "conversation_created", "id": 991, "created_at": "2026-10-12T00:00:00Z"}).encode()
    assert client.post("/webhooks/inbox/wrong", content=body).status_code == 401
    assert client.post("/webhooks/inbox/tok123", content=body).status_code == 200
    assert client.post("/webhooks/inbox/tok123", content=body).status_code == 200  # duplicate accepted, stored once
    ev = v.normalize_inbox_webhook(body, "amrit_dairy", "static_token_hint")
    assert ev.event_type == "bitespeed_inbox.conversation_created" and ev.auth_level == "static_token_hint"
