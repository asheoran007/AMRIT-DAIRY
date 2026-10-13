"""Read-only audit: customer records that share one phone number (duplicates). Prints COUNTS and ids only - no names,
phones or addresses - so the output is safe to paste anywhere. Never merges anything (merging needs identity + history
confirmation by the owner; see DEPLOYMENT.md).

Usage (server, where the Shopify token lives):  python -m amrit_adapter.tools.audit_customers [--ids]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from .register_shopify_webhooks import gql

PAGE = """query($after:String){ customers(first:250, after:$after, sortKey:CREATED_AT){ pageInfo{ hasNextPage endCursor }
  nodes{ id numberOfOrders phone defaultAddress{ phone } addresses(first:3){ phone } } } }"""


def _digits(p: str | None) -> str:
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def collect() -> dict[str, list[dict]]:
    by_phone: dict[str, list[dict]] = defaultdict(list)
    after = None
    while True:
        data = gql(PAGE, {"after": after})["customers"]
        for c in data["nodes"]:
            keys = {_digits(c.get("phone")), _digits((c.get("defaultAddress") or {}).get("phone"))} | {_digits(a.get("phone")) for a in c.get("addresses") or []}
            keys.discard("")
            for k in keys:
                by_phone[k].append({"id": c["id"], "orders": int(c.get("numberOfOrders") or 0)})
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return by_phone


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", action="store_true", help="also print the customer gids per duplicate group")
    a = ap.parse_args(argv)
    by_phone = collect()
    dups = {k: sorted({c["id"]: c for c in v}.values(), key=lambda c: -c["orders"]) for k, v in by_phone.items() if len({c["id"] for c in v}) > 1}
    print(f"phones seen: {len(by_phone)}; phones with >1 customer record: {len(dups)}; "
          f"records involved: {sum(len(v) for v in dups.values())}; groups where >1 record has orders: "
          f"{sum(1 for v in dups.values() if sum(1 for c in v if c['orders']) > 1)}")
    if a.ids:
        for k, v in sorted(dups.items(), key=lambda kv: -len(kv[1])):
            print(f"  phone ****{k[-4:]}: " + ", ".join(f"{c['id'].rsplit('/', 1)[1]}({c['orders']} orders)" for c in v))
    print("engine rule: find_customer_by_phone prefers the record with order history, then one with an email; it never creates a new record "
          "when one exists. Merging is an owner decision (Shopify admin -> Customers -> Merge) after confirming identity and history.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
