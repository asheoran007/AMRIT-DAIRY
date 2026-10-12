"""Shopify Admin GraphQL client (2026-10) — only the operations the engine needs.

Operations (names from Shopify docs; validated against the pinned schema when the connector is available):
- customers(query:"phone:+91...")          -> customer id + addresses
- draftOrderCreate(input)                  -> draft order id + invoiceUrl (secure Shopify checkout; pays via the store's
                                              configured gateway = Razorpay; completion creates the real order)
- draftOrderInvoiceSend is NOT used (we deliver the URL on WhatsApp ourselves)
- order(id) / orders(query:"name:#1150")   -> verify financial_status after the orders/paid webhook

Never charges a customer; a draft order is an unpaid quote until the customer completes checkout.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

CUSTOMER_BY_PHONE = """
query($q:String!){ customers(first:3, query:$q){ nodes{ id displayName phone email numberOfOrders
  defaultAddress{ id name address1 address2 city province zip country phone }
  addresses(first:5){ id name address1 address2 city province zip country phone } } } }"""

DRAFT_ORDER_CREATE = """
mutation($input:DraftOrderInput!){ draftOrderCreate(input:$input){ draftOrder{ id name invoiceUrl totalPriceSet{ shopMoney{ amount currencyCode } } status }
  userErrors{ field message } } }"""

DRAFT_ORDER_CALCULATE = """
mutation($input:DraftOrderInput!){ draftOrderCalculate(input:$input){ calculatedDraftOrder{ currencyCode taxesIncluded
  totalPriceSet{ shopMoney{ amount } } subtotalPriceSet{ shopMoney{ amount } } totalTaxSet{ shopMoney{ amount } }
  totalShippingPriceSet{ shopMoney{ amount } } totalDiscountsSet{ shopMoney{ amount } }
  availableShippingRates{ handle title price{ amount } }
  lineItems{ quantity variant{ id availableForSale inventoryQuantity inventoryPolicy price } originalUnitPriceSet{ shopMoney{ amount } } }
  warnings{ message } alerts{ title } } userErrors{ field message } } }"""

VARIANTS_BY_ID = """
query($ids:[ID!]!){ nodes(ids:$ids){ ... on ProductVariant{ id title price availableForSale inventoryQuantity inventoryPolicy product{ status } } } }"""

ORDER_BY_ID = """
query($id:ID!){ order(id:$id){ id name displayFinancialStatus displayFulfillmentStatus currentTotalPriceSet{ shopMoney{ amount currencyCode } }
  customer{ id } lineItems(first:10){ nodes{ variant{ id } quantity } } } }"""


@dataclass
class Address:
    name: str
    address1: str
    city: str
    province: str
    zip: str
    phone: str
    address2: str = ""
    country: str = "India"
    id: str | None = None

    def summary_hi(self) -> str:
        line2 = f", {self.address2}" if self.address2 else ""
        return f"{self.name}, {self.address1}{line2}, {self.city}, {self.province} – {self.zip}"

    def to_shopify(self) -> dict[str, Any]:
        return {"firstName": self.name.split(" ")[0], "lastName": " ".join(self.name.split(" ")[1:]) or ".",
                "address1": self.address1, "address2": self.address2 or None, "city": self.city, "province": self.province,
                "zip": self.zip, "country": self.country, "phone": self.phone}


@dataclass
class QuoteCalc:
    """Shopify's own calculation for exactly the lines/address/discount we will put on the draft (draftOrderCalculate)."""
    currency: str
    taxes_included: bool
    unit_minor: int
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    shipping_minor: int
    shipping_handle: str | None
    shipping_title: str | None
    total_minor: int
    available: bool
    inventory_qty: int | None
    inventory_policy: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class CustomerRecord:
    id: str | None
    phone: str
    addresses: list[Address] = field(default_factory=list)
    orders_count: int = 0


@dataclass
class DraftOrder:
    id: str
    name: str
    invoice_url: str
    total_minor: int
    currency: str


class ShopifyClient(Protocol):
    def find_customer_by_phone(self, phone_e164: str) -> CustomerRecord | None: ...
    def create_draft_order(self, *, variant_id: str, quantity: int, address: Address, phone: str, quote_ref: str,
                           customer_id: str | None, note: str, discount_minor: int = 0, discount_title: str | None = None) -> DraftOrder: ...
    def get_order(self, order_gid: str) -> dict[str, Any] | None: ...
    def calculate_quote(self, *, variant_id: str, quantity: int, address: Address, customer_id: str | None,
                        discount_minor: int = 0, discount_title: str | None = None) -> QuoteCalc: ...
    def get_variants(self, variant_ids: list[str]) -> dict[str, dict[str, Any]]: ...


def _minor(x: Any) -> int:
    return int(round(float(x) * 100))


def _discount_input(discount_minor: int, discount_title: str | None, quote_ref: str | None) -> dict[str, Any]:
    return {"valueType": "FIXED_AMOUNT", "value": discount_minor / 100,
            "amountWithCurrency": {"amount": f"{discount_minor / 100:.2f}", "currencyCode": "INR"},
            "title": (discount_title or "WhatsApp offer")[:255], "description": f"quote {quote_ref}" if quote_ref else "WhatsApp channel offer"}


def _pick_rate(rates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Cheapest available shipping rate for this address; None when Shopify offers no rate (then we must not quote)."""
    return min(rates, key=lambda r: float(r["price"]["amount"])) if rates else None


class LiveShopifyClient:
    """Admin GraphQL 2026-10. Auth via TokenProvider (static offline token or Dev Dashboard client-credentials, auto-renewed).
    Recovery: 401 -> one forced token renewal + retry; 429/5xx/network -> exponential backoff (3 attempts); errors logged without secrets."""

    def __init__(self, shop_domain: str, access_token: str = "", api_version: str = "2026-10", auth: "TokenProvider | None" = None) -> None:
        from .shopify_auth import TokenProvider
        self.auth = auth or TokenProvider(shop_domain, static_token=access_token)
        self.url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"

    def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        import logging, time
        log = logging.getLogger("amrit.shopify")
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = httpx.post(self.url, headers={"X-Shopify-Access-Token": self.auth.token(force_refresh=attempt > 0 and last is not None and "401" in str(last)),
                                                  "Content-Type": "application/json"},
                               json={"query": query, "variables": variables}, timeout=20)
            except httpx.HTTPError as exc:            # network / timeout
                last = exc; log.warning("shopify network error attempt=%s: %s", attempt + 1, type(exc).__name__)
                time.sleep(0.5 * 2 ** attempt); continue
            if r.status_code == 401:
                last = RuntimeError("401 unauthorized"); log.warning("shopify 401 attempt=%s -> renewing token", attempt + 1); continue
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(float(r.headers.get("Retry-After") or 0.5 * 2 ** attempt)); continue
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                msg = json.dumps(body["errors"])[:500]
                if "THROTTLED" in msg and attempt < 2:
                    last = RuntimeError(msg); time.sleep(1.0 * 2 ** attempt); continue
                raise RuntimeError(msg)
            return body["data"]
        raise RuntimeError(f"shopify request failed after retries: {last}")

    def find_customer_by_phone(self, phone_e164: str) -> CustomerRecord | None:
        data = self._gql(CUSTOMER_BY_PHONE, {"q": f"phone:{phone_e164}"})
        nodes = data["customers"]["nodes"]
        if not nodes:
            return None
        # Duplicate customer records for one phone exist in the live store (observed 2026-10-12: 3 x "AMIT KUMAR").
        # Prefer the record with order history, then one with an email; never create a 4th.
        c = sorted(nodes, key=lambda n: (-int(n.get("numberOfOrders") or 0), 0 if n.get("email") else 1))[0]
        addrs = [Address(a.get("name") or c["displayName"], a.get("address1") or "", a.get("city") or "", a.get("province") or "",
                         a.get("zip") or "", a.get("phone") or phone_e164, a.get("address2") or "", a.get("country") or "India", a["id"])
                 for a in (c.get("addresses") or []) if a.get("address1") and a.get("zip")]
        return CustomerRecord(c["id"], phone_e164, addrs, int(c.get("numberOfOrders") or 0))

    def create_draft_order(self, *, variant_id, quantity, address, phone, quote_ref, customer_id, note,
                           discount_minor: int = 0, discount_title: str | None = None, shipping_handle: str | None = None) -> DraftOrder:
        inp: dict[str, Any] = {
            "lineItems": [{"variantId": variant_id, "quantity": quantity}],
            "shippingAddress": address.to_shopify(), "billingAddress": address.to_shopify(),
            "note": note, "tags": ["whatsapp", "amrit-engine", f"quote:{quote_ref}"],
            "customAttributes": [{"key": "quote_ref", "value": quote_ref}, {"key": "source", "value": "whatsapp_engine"}],
            "useCustomerDefaultAddress": False,
        }
        if discount_minor > 0:   # owner-approved channel offer, shown as a titled discount line on the Shopify invoice
            inp["appliedDiscount"] = _discount_input(discount_minor, discount_title, quote_ref)
        if shipping_handle:      # the rate Shopify itself offered in calculate_quote -> invoice total == quoted total
            inp["shippingLine"] = {"shippingRateHandle": shipping_handle}
        if customer_id:
            inp["customerId"] = customer_id
        else:
            inp["phone"] = phone
        data = self._gql(DRAFT_ORDER_CREATE, {"input": inp})
        res = data["draftOrderCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"draftOrderCreate userErrors: {res['userErrors']}")
        d = res["draftOrder"]
        money = d["totalPriceSet"]["shopMoney"]
        return DraftOrder(d["id"], d["name"], d["invoiceUrl"], int(round(float(money["amount"]) * 100)), money["currencyCode"])

    def get_order(self, order_gid: str) -> dict[str, Any] | None:
        return self._gql(ORDER_BY_ID, {"id": order_gid}).get("order")

    def calculate_quote(self, *, variant_id, quantity, address, customer_id, discount_minor=0, discount_title=None) -> QuoteCalc:
        inp: dict[str, Any] = {"lineItems": [{"variantId": variant_id, "quantity": quantity}],
                               "shippingAddress": address.to_shopify(), "useCustomerDefaultAddress": False}
        if customer_id:
            inp["customerId"] = customer_id
        if discount_minor > 0:
            inp["appliedDiscount"] = _discount_input(discount_minor, discount_title, None)
        res = self._gql(DRAFT_ORDER_CALCULATE, {"input": inp})["draftOrderCalculate"]
        if res["userErrors"]:
            raise RuntimeError(f"draftOrderCalculate userErrors: {res['userErrors']}")
        c = res["calculatedDraftOrder"]
        li = c["lineItems"][0]; v = li.get("variant") or {}
        rate = _pick_rate(c["availableShippingRates"])
        ship_minor = _minor(rate["price"]["amount"]) if rate else 0
        warnings = [w["message"] for w in c.get("warnings") or []] + [a["title"] for a in c.get("alerts") or []]
        # draftOrderCalculate's totalPrice excludes shipping until a shippingLine is set -> add the chosen rate explicitly
        return QuoteCalc(c["currencyCode"], bool(c["taxesIncluded"]), _minor(li["originalUnitPriceSet"]["shopMoney"]["amount"]),
                         _minor(c["subtotalPriceSet"]["shopMoney"]["amount"]), _minor(c["totalDiscountsSet"]["shopMoney"]["amount"]),
                         _minor(c["totalTaxSet"]["shopMoney"]["amount"]), ship_minor, rate["handle"] if rate else None, rate["title"] if rate else None,
                         _minor(c["totalPriceSet"]["shopMoney"]["amount"]) + ship_minor, bool(v.get("availableForSale")),
                         v.get("inventoryQuantity"), v.get("inventoryPolicy") or "DENY", warnings)

    def get_variants(self, variant_ids):
        nodes = self._gql(VARIANTS_BY_ID, {"ids": variant_ids})["nodes"]
        return {n["id"]: n for n in nodes if n}

    def create_test_draft(self, *, amount_minor: int, address: Address, phone: str, quote_ref: str, customer_id: str | None) -> DraftOrder:
        """₹-small custom line item (no product, no inventory) used ONLY for the gateway/webhook round-trip test."""
        inp: dict[str, Any] = {
            "lineItems": [{"title": "Engine gateway test (no goods shipped)", "originalUnitPriceWithCurrency": {"amount": f"{amount_minor / 100:.2f}", "currencyCode": "INR"},
                           "quantity": 1, "requiresShipping": False, "taxable": False}],
            "shippingAddress": address.to_shopify(), "billingAddress": address.to_shopify(),
            "note": f"GATEWAY TEST {quote_ref} — do not ship", "tags": ["whatsapp", "amrit-engine", "test", f"quote:{quote_ref}"],
            "customAttributes": [{"key": "quote_ref", "value": quote_ref}, {"key": "source", "value": "whatsapp_engine_test"}],
            "useCustomerDefaultAddress": False,
        }
        if customer_id:
            inp["customerId"] = customer_id
        else:
            inp["phone"] = phone
        res = self._gql(DRAFT_ORDER_CREATE, {"input": inp})["draftOrderCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"draftOrderCreate userErrors: {res['userErrors']}")
        d = res["draftOrder"]; money = d["totalPriceSet"]["shopMoney"]
        return DraftOrder(d["id"], d["name"], d["invoiceUrl"], int(round(float(money["amount"]) * 100)), money["currencyCode"])


class FakeShopifyClient:
    """Deterministic test double used by the MOCK test sequence."""

    def __init__(self, customers: dict[str, CustomerRecord] | None = None) -> None:
        self.customers = customers or {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self._n = 0

    def find_customer_by_phone(self, phone_e164):
        return self.customers.get(phone_e164)

    PRICES = {"gid://shopify/ProductVariant/1": 350000, "gid://shopify/ProductVariant/5": 1750000, "gid://shopify/ProductVariant/10": 3300000,
              "gid://shopify/ProductVariant/h250": 40000, "gid://shopify/ProductVariant/h500": 80000, "gid://shopify/ProductVariant/h1000": 150000,
              # live Amrit Dairy ids (observed 12 Oct 2026) so the synced config also works against the fake
              "gid://shopify/ProductVariant/49099713773751": 350000, "gid://shopify/ProductVariant/49108524466359": 1750000,
              "gid://shopify/ProductVariant/49108524499127": 3300000, "gid://shopify/ProductVariant/49290907713719": 40000,
              "gid://shopify/ProductVariant/49290907746487": 80000, "gid://shopify/ProductVariant/49099713970359": 150000}
    shipping_minor = 0            # tests can set a paid rate
    stock: dict[str, int] = {}    # variant -> inventory (default plenty)
    unavailable: set[str] = set()

    def get_variants(self, variant_ids):
        return {v: {"id": v, "price": f"{self.PRICES.get(v, 1) / 100:.2f}", "availableForSale": v not in self.unavailable,
                    "inventoryQuantity": self.stock.get(v, 999), "inventoryPolicy": "DENY", "product": {"status": "ACTIVE"}} for v in variant_ids}

    def calculate_quote(self, *, variant_id, quantity, address, customer_id, discount_minor=0, discount_title=None):
        unit = self.PRICES.get(variant_id, 100); sub = unit * quantity; total = sub - discount_minor + self.shipping_minor
        tax = int(round((sub - discount_minor) * 18 / 118))     # GST included in price (live shop: taxesIncluded=true)
        return QuoteCalc("INR", True, unit, sub, discount_minor, tax, self.shipping_minor, "rate-std" if self.shipping_minor == 0 else "rate-paid",
                         "Standard", total, variant_id not in self.unavailable, self.stock.get(variant_id, 999), "DENY", [])

    def create_draft_order(self, *, variant_id, quantity, address, phone, quote_ref, customer_id, note, discount_minor=0, discount_title=None, shipping_handle=None):
        for d in self.drafts.values():
            if d["quote_ref"] == quote_ref:  # Shopify itself would create a second draft; our caller must prevent that
                raise AssertionError("duplicate draft order for same quote_ref reached Shopify")
        self._n += 1
        gid = f"gid://shopify/DraftOrder/{1000 + self._n}"
        price = self.PRICES.get(variant_id, 100)
        total = price * quantity - int(discount_minor or 0) + (self.shipping_minor if shipping_handle else 0)   # order-level discount once + chosen rate
        self.drafts[gid] = {"quote_ref": quote_ref, "variant_id": variant_id, "quantity": quantity, "total": total, "address": address,
                            "discount": int(discount_minor or 0), "discount_title": discount_title}
        return DraftOrder(gid, f"#D{self._n}", f"https://kmaz1y-dq.myshopify.com/invoices/test-{self._n}", total, "INR")

    def create_test_draft(self, *, amount_minor, address, phone, quote_ref, customer_id):
        self._n += 1
        gid = f"gid://shopify/DraftOrder/{1000 + self._n}"
        self.drafts[gid] = {"quote_ref": quote_ref, "variant_id": None, "quantity": 1, "total": amount_minor, "address": address, "discount": 0, "discount_title": None}
        return DraftOrder(gid, f"#D{self._n}", f"https://kmaz1y-dq.myshopify.com/invoices/test-{self._n}", amount_minor, "INR")

    def complete_checkout(self, draft_gid: str, order_number: int) -> dict[str, Any]:
        """Simulates the customer paying on the invoice: Shopify creates a real order."""
        d = self.drafts[draft_gid]
        gid = f"gid://shopify/Order/{order_number}"
        self.orders[gid] = {"id": gid, "name": f"#{order_number}", "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
                            "currentTotalPriceSet": {"shopMoney": {"amount": f"{d['total']/100:.2f}", "currencyCode": "INR"}},
                            "lineItems": {"nodes": [{"variant": ({"id": d["variant_id"]} if d["variant_id"] else None), "quantity": d["quantity"]}]}, "draft_order_id": draft_gid}
        return self.orders[gid]

    def get_order(self, order_gid):
        return self.orders.get(order_gid)
