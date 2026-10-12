"""Register (idempotently) the Shopify webhook subscriptions the adapter consumes, on the custom app whose token is in .env.

Usage (after deploy):  python -m amrit_adapter.tools.register_shopify_webhooks https://adapter.amritdairy.in [--dry-run] [--prune]

Shopify signs deliveries from an app-created subscription with THAT app's client secret -> the adapter verifies with
SHOPIFY_WEBHOOK_SECRET, or SHOPIFY_CLIENT_SECRET when that is blank (Dev Dashboard app credentials).
Operations validated against the Admin GraphQL 2026-10 schema on 12 Oct 2026: webhookSubscriptions, webhookSubscriptionCreate,
webhookSubscriptionDelete.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from ..config import settings

TOPICS = {  # topic -> path on the adapter
    "ORDERS_PAID": "/webhooks/shopify",
    "ORDERS_CREATE": "/webhooks/shopify",
    "ORDERS_UPDATED": "/webhooks/shopify",
    "ORDERS_CANCELLED": "/webhooks/shopify",
    "CHECKOUTS_UPDATE": "/webhooks/shopify",
    "FULFILLMENTS_CREATE": "/webhooks/shopify",
    "FULFILLMENTS_UPDATE": "/webhooks/shopify",
}
LIST = "query{ webhookSubscriptions(first:50){ nodes{ id topic uri } } }"
CREATE = ("mutation($topic:WebhookSubscriptionTopic!,$sub:WebhookSubscriptionInput!){ webhookSubscriptionCreate(topic:$topic, webhookSubscription:$sub)"
          "{ webhookSubscription{ id topic } userErrors{ field message } } }")
DELETE = "mutation($id:ID!){ webhookSubscriptionDelete(id:$id){ deletedWebhookSubscriptionId userErrors{ field message } } }"


_auth = None


def gql(query: str, variables: dict | None = None) -> dict:
    global _auth
    if _auth is None:
        _auth = settings.shopify_token_provider()
    url = f"https://{settings.shopify_shop_domain}/admin/api/2026-10/graphql.json"
    r = httpx.post(url, headers={"X-Shopify-Access-Token": _auth.token(), "Content-Type": "application/json"},
                   json={"query": query, "variables": variables or {}}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise SystemExit(f"GraphQL errors: {json.dumps(body['errors'])[:500]}")
    return body["data"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", help="public https base of the adapter, e.g. https://adapter.amritdairy.in")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--prune", action="store_true", help="delete this app's subscriptions that point elsewhere than base_url")
    a = ap.parse_args(argv)
    if not settings.shopify_configured:
        print("SHOPIFY_SHOP_DOMAIN + (SHOPIFY_CLIENT_ID/SECRET or SHOPIFY_ADMIN_TOKEN) missing in .env", file=sys.stderr)
        return 2
    if not a.base_url.startswith("https://"):
        print("base_url must be https://", file=sys.stderr)
        return 2
    existing = gql(LIST)["webhookSubscriptions"]["nodes"]   # only THIS app's subscriptions are visible/affected
    have = {(n["topic"], n["uri"]) for n in existing}
    plan_create = [(t, a.base_url.rstrip("/") + p) for t, p in TOPICS.items() if (t, a.base_url.rstrip("/") + p) not in have]
    plan_delete = [n for n in existing if a.prune and not n["uri"].startswith(a.base_url)]
    print("existing:", json.dumps(existing, indent=1))
    print("create:", plan_create)
    print("delete:", [(n["topic"], n["uri"]) for n in plan_delete])
    if a.dry_run:
        return 0
    for topic, uri in plan_create:
        res = gql(CREATE, {"topic": topic, "sub": {"uri": uri, "format": "JSON"}})["webhookSubscriptionCreate"]
        if res["userErrors"]:
            print("ERROR", topic, res["userErrors"]); return 1
        print("created", topic, res["webhookSubscription"]["id"])
    for n in plan_delete:
        res = gql(DELETE, {"id": n["id"]})["webhookSubscriptionDelete"]
        print("deleted" if not res["userErrors"] else f"ERROR {res['userErrors']}", n["topic"], n["uri"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
