"""Product configuration: one JSON file generated from the Amrit Shopify store (no hard-coded per-product flows)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "products_amrit.json"


@dataclass
class Variant:
    label: str
    label_hi: str
    variant_id: str | None
    sku: str | None
    price_minor: int | None
    available: bool | None
    offer_minor: int = 0            # owner-approved per-unit channel discount (WhatsApp only); 0 = none
    offer_title: str | None = None  # title shown on the Shopify draft-order discount line

    @property
    def net_minor(self) -> int | None:
        """Price the customer actually pays per unit on this channel (list price minus approved offer)."""
        return None if self.price_minor is None else max(self.price_minor - self.offer_minor, 0)


@dataclass
class Product:
    key: str
    title: str
    title_hi: str
    keywords: tuple[str, ...]
    short_hi: str
    video_url: str | None
    image_url: str | None
    media_approved: bool
    delivery_class: str          # courier | local_only | registry
    restricted: bool
    approved_for_sale: bool
    verified_from_shopify: bool
    product_id: str | None
    variants: tuple[Variant, ...] = field(default_factory=tuple)

    @property
    def sellable(self) -> bool:
        """Quotable only when approved, verified against Shopify and at least one variant has an id and a price."""
        return self.approved_for_sale and self.verified_from_shopify and any(v.variant_id and v.price_minor for v in self.variants)


class Catalog:
    def __init__(self, products: list[Product], brand_id: str, store_domain: str, tax_inclusive: bool, brand_video_url: str | None = None):
        self.products = {p.key: p for p in products}
        self.brand_id, self.store_domain, self.tax_inclusive = brand_id, store_domain, tax_inclusive
        self.brand_video_url = brand_video_url   # owner-approved link (e.g. YouTube) sent as text; never a fabricated media file

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "Catalog":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        prods = []
        offers_all = raw.get("channel_offers") or {}
        for p in raw["products"]:
            media = p.get("media") or {}
            offers = {_norm(k): v for k, v in (offers_all.get(p["key"]) or {}).items() if isinstance(v, dict)}

            def _offer(label: str) -> tuple[int, str | None]:
                o = offers.get(_norm(label))
                if not o or not o.get("approved"):   # unapproved / pending rules never change a price
                    return 0, None
                return int(o.get("per_unit_minor") or 0), o.get("title")

            prods.append(Product(
                key=p["key"], title=p["title"], title_hi=p.get("title_hi", p["title"]),
                keywords=tuple(k.lower() for k in p.get("keywords", [])), short_hi=p.get("short_hi", ""),
                video_url=media.get("video_url"), image_url=media.get("image_url"), media_approved=bool(media.get("approved")),
                delivery_class=p.get("delivery_class", "courier"), restricted=bool(p.get("restricted")),
                approved_for_sale=bool(p.get("approved_for_sale")), verified_from_shopify=bool(p.get("verified_from_shopify")),
                product_id=p.get("product_id"),
                variants=tuple(Variant(v["label"], v.get("label_hi", v["label"]), v.get("variant_id"), v.get("sku"),
                                       v.get("price_minor"), v.get("available"), *_offer(v["label"])) for v in p.get("variants", [])),
            ))
        bv = raw.get("brand_video") or {}
        return cls(prods, raw["brand_id"], raw["store_domain"], bool(raw.get("prices_tax_inclusive", True)),
                   bv.get("url") if bv.get("approved") else None)

    def get(self, key: str) -> Product | None:
        return self.products.get(key)

    # ---- live refresh: the JSON file is only a bootstrap; prices/availability come from Shopify, re-read every `ttl_s` ----
    refreshed_at: float = 0.0
    refresh_error: str | None = None

    def refresh_live(self, shopify, ttl_s: int = 600, now: float | None = None) -> bool:
        """Overwrite variant price/availability from Shopify (nodes by id). Returns True when fresh data is in place.
        On failure keeps the last known values and records refresh_error; callers decide whether stale data may be shown."""
        import time
        now = now or time.time()
        if now - self.refreshed_at < ttl_s:
            return True
        ids = [v.variant_id for p in self.products.values() for v in p.variants if v.variant_id]
        if not ids or not hasattr(shopify, "get_variants"):
            return False
        try:
            live = shopify.get_variants(ids)
        except Exception as exc:  # network/auth: keep last values, mark stale
            self.refresh_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return False
        for p in self.products.values():
            for v in p.variants:
                n = live.get(v.variant_id or "")
                if not n:
                    v.available = False      # variant vanished from Shopify -> never quotable
                    continue
                v.price_minor = int(round(float(n["price"]) * 100))
                v.available = bool(n.get("availableForSale")) and (n.get("product") or {}).get("status", "ACTIVE") == "ACTIVE"
        self.refreshed_at, self.refresh_error = now, None
        return True

    @property
    def stale(self) -> bool:
        return self.refreshed_at == 0.0

    def sellable(self) -> list[Product]:
        return [p for p in self.products.values() if p.sellable]


# ---- Shopify sync (uses Admin GraphQL; token from env; run manually after the connector/custom app is approved) ----
SYNC_QUERY = """
query($first:Int!,$after:String){ products(first:$first, after:$after, query:"status:active"){
  pageInfo{hasNextPage endCursor}
  nodes{ id title handle status totalInventory
    media(first:10){ nodes{ mediaContentType ... on Video{ sources{ url mimeType } } ... on MediaImage{ image{ url } } } }
    variants(first:20){ nodes{ id sku title price availableForSale inventoryQuantity } } } } }
"""


def merge_shopify(raw_cfg: dict[str, Any], shopify_products: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure function: merge a Shopify products payload into the config by title keyword match. Marks verified_from_shopify
    only when every configured variant label matched a Shopify variant title. Owner-approved media flag is never set here."""
    for p in raw_cfg["products"]:
        if p.get("delivery_class") == "registry":
            continue  # cows are never Shopify products
        match = next((sp for sp in shopify_products if p.get("product_id") and sp["id"] == p["product_id"]), None)
        if not match:
            match = next((sp for sp in shopify_products if p["key"] in sp["title"].lower().split()), None)
        if not match:
            p["verified_from_shopify"] = False
            continue
        p["product_id"] = match["id"]
        p["title"] = match["title"]
        vids = [m for m in (match.get("media", {}).get("nodes") or []) if m.get("mediaContentType") == "VIDEO" and m.get("sources")]
        imgs = [m for m in (match.get("media", {}).get("nodes") or []) if m.get("mediaContentType") == "IMAGE"]
        p.setdefault("media", {})
        p["media"]["video_url"] = vids[0]["sources"][0]["url"] if vids else None
        p["media"]["image_url"] = imgs[0]["image"]["url"] if imgs else None
        all_matched = True
        for v in p.get("variants", []):
            sv = next((x for x in match["variants"]["nodes"] if _norm(x["title"]) == _norm(v["label"])), None)
            if not sv:
                all_matched = False
                continue
            v["variant_id"] = sv["id"]; v["sku"] = sv.get("sku"); v["price_minor"] = int(round(float(sv["price"]) * 100))
            v["available"] = bool(sv.get("availableForSale"))
        p["verified_from_shopify"] = all_matched and bool(p.get("variants"))
    return raw_cfg


def _norm(s: str) -> str:
    s = s.lower().replace(" ", "")
    for a, b in (("litre", "l"), ("liter", "l"), ("ltr", "l"), ("kilo", "kg"), ("gram", "g"), ("gm", "g")):
        s = s.replace(a, b)
    return s
