from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Mapping

from ..envelope import InternalEvent


class WebhookRejected(Exception):
    """Raised when a webhook fails authentication. Caller returns 401 and stores nothing."""


def _h(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


# ---------------- Shopify ----------------

def verify_shopify(raw_body: bytes, headers: Mapping[str, str], secret: str) -> None:
    if not secret:
        raise WebhookRejected("shopify secret not configured")
    sent = _h(headers, "X-Shopify-Hmac-SHA256") or ""
    digest = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(sent, digest):
        raise WebhookRejected("shopify hmac mismatch")


def normalize_shopify(raw_body: bytes, headers: Mapping[str, str], brand_id: str) -> InternalEvent:
    topic = _h(headers, "X-Shopify-Topic") or "unknown"
    webhook_id = _h(headers, "X-Shopify-Webhook-Id") or ""
    event_id = _h(headers, "X-Shopify-Event-Id") or webhook_id
    if not event_id:
        raise WebhookRejected("shopify delivery without event/webhook id")
    body: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    obj_id = str(body.get("id") or body.get("admin_graphql_api_id") or "")
    occurred = body.get("updated_at") or body.get("created_at") or _h(headers, "X-Shopify-Triggered-At") or ""
    return InternalEvent(
        brand_id=brand_id,
        source="shopify",
        source_event_id=f"{topic}:{event_id}",
        source_object_id=obj_id,
        event_type=f"shopify.{topic.replace('/', '.')}",
        occurred_at=occurred,
        auth_level="verified_signature",
        payload=body,
    )


# ---------------- Razorpay ----------------

def verify_razorpay(raw_body: bytes, headers: Mapping[str, str], secret: str) -> None:
    if not secret:
        raise WebhookRejected("razorpay secret not configured")
    sent = _h(headers, "X-Razorpay-Signature") or ""
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sent, digest):
        raise WebhookRejected("razorpay signature mismatch")


def normalize_razorpay(raw_body: bytes, headers: Mapping[str, str], brand_id: str) -> InternalEvent:
    event_id = _h(headers, "x-razorpay-event-id")
    body: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    event = body.get("event", "unknown")
    if not event_id:
        raise WebhookRejected("razorpay delivery without x-razorpay-event-id")
    payload = body.get("payload", {})
    payment = (payload.get("payment") or {}).get("entity") or {}
    link = (payload.get("payment_link") or {}).get("entity") or {}
    obj_id = payment.get("id") or link.get("id") or ""
    created = payment.get("created_at") or link.get("updated_at") or body.get("created_at") or 0
    return InternalEvent(
        brand_id=brand_id,
        source="razorpay",
        source_event_id=event_id,
        source_object_id=str(obj_id),
        event_type=f"razorpay.{event}",
        occurred_at=str(created),
        auth_level="verified_signature",
        payload=body,
    )


# ---------------- BiteSpeed support inbox (Chatwoot-style webhooks) ----------------

def verify_inbox_webhook(path_token: str, expected_token: str) -> str:
    """Support-console webhooks (Settings -> Integrations -> Webhooks) carry no signature; the only
    control is an unguessable token embedded in the URL path. Treated as static_token_hint."""
    if not expected_token:
        # No token configured (e.g. during a rotation) -> accept nothing: the engine must never run on unauthenticated input.
        raise WebhookRejected("inbox webhook token not configured")
    if not hmac.compare_digest(path_token or "", expected_token):
        raise WebhookRejected("inbox webhook token mismatch")
    return "static_token_hint"


def normalize_inbox_webhook(raw_body: bytes, brand_id: str, auth_level: str) -> InternalEvent:
    body: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    event = str(body.get("event") or "unknown")          # e.g. conversation_created, message_created
    obj_id = str(body.get("id") or (body.get("conversation") or {}).get("id") or "")
    created = str(body.get("created_at") or "")
    if not obj_id:
        raise WebhookRejected("inbox payload without id")
    # Dedupe key = event + object + created_at + digest of the delivered body. Live finding 16 Oct: incoming customer
    # messages arrive with the message id/created_at missing at top level, so "event:conversation_id:" collided with
    # every earlier incoming message and the engine never ran. Identical redeliveries still dedupe (same body).
    digest = hashlib.sha256(raw_body).hexdigest()[:16]
    return InternalEvent(
        brand_id=brand_id,
        source="bitespeed_inbox",
        source_event_id=f"{event}:{obj_id}:{created}:{digest}",
        source_object_id=obj_id,
        event_type=f"bitespeed_inbox.{event}",
        occurred_at=created,
        auth_level=auth_level,  # type: ignore[arg-type]
        payload=body,
    )


# ---------------- Shiprocket ----------------

def verify_shiprocket(headers: Mapping[str, str], token: str) -> str:
    """Returns the auth level. Static token only => 'static_token_hint'. Never 'verified_signature'."""
    if not token:
        return "unauthenticated"
    sent = _h(headers, "x-api-key") or ""
    if not hmac.compare_digest(sent, token):
        raise WebhookRejected("shiprocket token mismatch")
    return "static_token_hint"


def normalize_shiprocket(raw_body: bytes, headers: Mapping[str, str], brand_id: str, auth_level: str) -> InternalEvent:
    body: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    awb = str(body.get("awb") or "")
    status_id = str(body.get("current_status_id") or body.get("shipment_status_id") or "")
    ts = str(body.get("current_timestamp") or "")
    if not awb:
        raise WebhookRejected("shiprocket payload without awb")
    # Shiprocket has no delivery id: derive a deterministic id from awb+status+timestamp.
    return InternalEvent(
        brand_id=brand_id,
        source="shiprocket",
        source_event_id=f"{awb}:{status_id}:{ts}",
        source_object_id=awb,
        event_type="shiprocket.tracking",
        occurred_at=ts,
        auth_level=auth_level,  # type: ignore[arg-type]
        payload=body,
    )
