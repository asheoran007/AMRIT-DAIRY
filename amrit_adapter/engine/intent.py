"""Hindi / English / Hinglish intent detection — deterministic, state-aware (brief §2, §10)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import Catalog

YES = {"haan", "ha", "han", "hanji", "haanji", "yes", "y", "ok", "okay", "theek", "thik", "sahi", "हाँ", "हां", "हा", "जी", "ठीक", "सही", "ji", "confirm", "✅"}
NO = {"nahi", "nahin", "na", "no", "nope", "नहीं", "नही", "ना", "galat", "wrong"}
CHANGE_ADDRESS = ["change address", "address change", "naya address", "address badal", "पता बदल", "dusra address", "different address", "new address", "edit address"]
LATER = ["baad mein", "baad me", "later", "बाद में", "abhi nahi", "not now"]
PAID = ["payment kar diya", "pay kar diya", "paid", "payment done", "paise bhej", "पैसे भेज", "भुगतान कर दिया", "payment ho gaya", "utr", "screenshot"]
TRACK = ["track", "tracking", "status", "kahan", "kaha hai", "कहाँ", "कहा", "awb", "courier", "delivery kab", "order status"]
HUMAN = ["human", "agent", "person", "baat karni", "baat karna", "call me", "व्यक्ति", "बात करनी", "team se", "customer care", "help", "मदद"]
STOP = ["stop", "unsubscribe", "बंद", "band karo", "mat bhejo", "रोक"]
MENU = ["hi", "hello", "hii", "hey", "namaste", "namaskar", "नमस्ते", "नमस्कार", "ram ram", "राम राम", "राम-राम", "menu", "start", "radhe radhe"]

QTY_WORDS = {"ek": 1, "एक": 1, "do": 2, "दो": 2, "teen": 3, "तीन": 3, "char": 4, "चार": 4, "paanch": 5, "पांच": 5, "पाँच": 5}


@dataclass(frozen=True)
class Intent:
    kind: str                 # product | yes | no | change_address | later | paid_claim | track | human | stop | menu | quantity | pack | unknown
    product_key: str | None = None
    quantity: int | None = None
    pack_label: str | None = None
    raw: str = ""


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[!?.,:;]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def detect(text: str, catalog: Catalog, state: str = "NEW", button_id: str | None = None) -> Intent:
    """button_id (from an interactive reply) always wins over free text."""
    if button_id:
        if button_id.startswith("product:"):
            return Intent("product", product_key=button_id.split(":", 1)[1], raw=button_id)
        if button_id.startswith("pack:"):
            return Intent("pack", pack_label=button_id.split(":", 1)[1], raw=button_id)
        if button_id.startswith("qty:"):
            return Intent("quantity", quantity=int(button_id.split(":", 1)[1]), raw=button_id)
        return Intent({"addr_yes": "yes", "addr_change": "change_address", "addr_later": "later", "confirm_yes": "yes",
                       "confirm_no": "no", "human": "human", "track": "track", "menu": "menu"}.get(button_id, "unknown"), raw=button_id)

    t = _norm(text)
    if not t:
        return Intent("unknown", raw=text)
    if any(w in t for w in STOP):
        return Intent("stop", raw=text)
    if any(w in t for w in HUMAN):
        return Intent("human", raw=text)
    if any(w in t for w in CHANGE_ADDRESS):
        return Intent("change_address", raw=text)
    if any(w in t for w in PAID):
        return Intent("paid_claim", raw=text)
    if any(w in t for w in TRACK) and state not in ("ADDRESS_COLLECT",):
        return Intent("track", raw=text)
    # products (any state: a customer may switch product mid-way)
    for p in catalog.products.values():
        if any(re.search(rf"(^|\W){re.escape(k)}(\W|$)", t) for k in p.keywords):
            qty = _quantity(t)
            pack = _pack(t, p)
            return Intent("product", product_key=p.key, quantity=qty, pack_label=pack, raw=text)
    # state-scoped interpretations
    if state in ("ADDRESS_CONFIRM", "QUOTE"):
        if any(w in t for w in LATER):
            return Intent("later", raw=text)
        if t in YES or t.split()[0] in YES:
            return Intent("yes", raw=text)
        if t in NO or t.split()[0] in NO:
            return Intent("no", raw=text)
    if state == "PACK":
        pk = _pack(t, None)
        if pk:
            return Intent("pack", pack_label=pk, raw=text)
    if state in ("QTY", "PACK"):
        q = _quantity(t)
        if q:
            return Intent("quantity", quantity=q, raw=text)
    if t in MENU or t.split()[0] in MENU:
        return Intent("menu", raw=text)
    return Intent("unknown", raw=text)


def _quantity(t: str) -> int | None:
    m = re.search(r"(\d+)\s*(pcs|piece|pc|qty|quantity|nos|x)?\b", t)
    if m and not re.search(r"\d+\s*(l|ltr|litre|liter|kg|g|gm|gram|ml)\b", t[m.start():m.end() + 6]):
        n = int(m.group(1))
        if 1 <= n <= 50:
            return n
    for w, n in QTY_WORDS.items():
        if re.search(rf"(^|\s){w}(\s|$)", t):
            return n
    return None


def _pack(t: str, product) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(l|ltr|litre|liter|kg|kilo|g|gm|gram)\b", t)
    if not m:
        return None
    n, u = m.group(1), m.group(2)
    u = {"ltr": "L", "litre": "L", "liter": "L", "l": "L", "kg": "kg", "kilo": "kg", "g": "g", "gm": "g", "gram": "g"}[u]
    n = n.rstrip("0").rstrip(".") if "." in n else n
    return f"{n} {u}"
