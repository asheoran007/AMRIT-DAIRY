"""Conversation state machine. Input: one inbound message. Output: message specs + side effects. Persisted per phone."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import audit, new_id, now_iso
from . import intent as intent_mod
from . import leads
from .catalog import Catalog, Product, _norm as _norm_label
from .shopify_client import Address, ShopifyClient

SESSION_TTL_H = 48


@dataclass
class Msg:
    """Channel-neutral message spec. type: text | buttons | list | video | image"""
    type: str
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)   # (id, title ≤20)
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (id, title ≤24, description ≤72)
    media_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {"type": self.type, "text": self.text}
        if self.buttons: d["buttons"] = [{"id": i, "title": t} for i, t in self.buttons]
        if self.rows: d["rows"] = [{"id": i, "title": t, "description": x} for i, t, x in self.rows]
        if self.media_url: d["media_url"] = self.media_url
        return d


@dataclass
class TurnResult:
    state: str
    messages: list[Msg]
    actions: list[str]
    handoff: bool = False
    handoff_team: str | None = None


def _load(conn: Connection, brand_id: str, phone: str) -> dict[str, Any]:
    row = conn.execute(text("SELECT * FROM conversation_session WHERE brand_id=:b AND phone_e164=:p"), {"b": brand_id, "p": phone}).fetchone()
    now = datetime.now(timezone.utc)
    if row and row.expires_at > now.isoformat():
        return dict(row._mapping)
    sid = new_id()
    conn.execute(text("DELETE FROM conversation_session WHERE brand_id=:b AND phone_e164=:p"), {"b": brand_id, "p": phone})
    conn.execute(text("""INSERT INTO conversation_session(session_id, brand_id, phone_e164, state, last_message_at, expires_at, updated_at)
                         VALUES (:s,:b,:p,'NEW',:n,:e,:n)"""),
                 {"s": sid, "b": brand_id, "p": phone, "n": now.isoformat(), "e": (now + timedelta(hours=SESSION_TTL_H)).isoformat()})
    return dict(conn.execute(text("SELECT * FROM conversation_session WHERE session_id=:s"), {"s": sid}).fetchone()._mapping)


def _save(conn: Connection, s: dict[str, Any]) -> None:
    s["updated_at"] = now_iso(); s["last_message_at"] = now_iso()
    cols = [k for k in s.keys() if k != "session_id"]
    conn.execute(text(f"UPDATE conversation_session SET {', '.join(f'{c}=:{c}' for c in cols)} WHERE session_id=:session_id"), s)


def _inr(minor: int) -> str:
    return f"₹{minor // 100:,}"


class Engine:
    def __init__(self, catalog: Catalog, shopify: ShopifyClient, brand_id: str = "amrit_dairy",
                 test_allowlist: tuple[str, ...] = (), test_payment_minor: int = 1000) -> None:
        self.cat, self.shop, self.brand = catalog, shopify, brand_id
        self.test_allowlist, self.test_payment_minor = tuple(test_allowlist), test_payment_minor
        self.price_ttl_s = 600   # pack-list prices re-read from Shopify at most every 10 min; the final quote is always live

    # ---------------- public ----------------
    def turn(self, conn: Connection, phone: str, text_in: str = "", button_id: str | None = None, display_name: str | None = None) -> TurnResult:
        r = self._turn(conn, phone, text_in, button_id, display_name)
        opts = [i for m in r.messages for i, _ in m.buttons] + [i for m in r.messages for i, _, _ in m.rows]
        conn.execute(text("UPDATE conversation_session SET last_options_json=:o WHERE brand_id=:b AND phone_e164=:p"),
                     {"o": json.dumps(opts) if opts else None, "b": self.brand, "p": phone})
        return r

    def _turn(self, conn: Connection, phone: str, text_in: str = "", button_id: str | None = None, display_name: str | None = None) -> TurnResult:
        s = _load(conn, self.brand, phone)
        contact_id = leads.ensure_contact(conn, self.brand, phone, display_name)
        s["contact_id"] = contact_id
        if not button_id:   # "2" typed in reply to a numbered list → the 2nd option the engine last presented
            button_id = _numbered_choice(text_in, s.get("last_options_json"))
        it = intent_mod.detect(text_in, self.cat, s["state"], button_id)
        audit(conn, "engine", "turn", s["session_id"], {"state": s["state"], "intent": it.kind, "product": it.product_key})
        actions: list[str] = []

        if (text_in or "").strip().upper() == "TESTPAY":        # gateway round-trip test, allowlisted numbers only, never a product
            return self._testpay(conn, s, actions)
        if it.kind == "stop":
            r = TurnResult("NEW", [Msg("text", "ठीक है जी 🙏 हम आपको प्रचार संदेश नहीं भेजेंगे — सिर्फ़ आपके ऑर्डर से जुड़ी ज़रूरी जानकारी मिलेगी।")], ["consent:withdrawn", "label:opt_out"])
            s["state"] = "NEW"; _save(conn, s); return r
        if it.kind == "human":
            s["state"] = "HANDOFF"; _save(conn, s)
            return TurnResult("HANDOFF", [Msg("text", "ठीक है जी 🙏 आपकी बात हमारी टीम तक पहुँचा रही हूँ — थोड़ी देर में एक व्यक्ति यहीं जवाब देगा।")], ["label:human_owner", "assign:Sales"], True, "Sales")
        if s["state"] == "HANDOFF":
            return TurnResult("HANDOFF", [], ["noop:human_owns"], True)
        if it.kind == "track":
            return self._track(conn, s, actions)
        if it.kind == "paid_claim":
            return self._paid_claim(conn, s, actions)
        if it.kind == "product":
            return self._product(conn, s, it, actions)
        if it.kind == "menu" or s["state"] == "NEW":
            _save(conn, s)
            return TurnResult(s["state"], [self._menu()], actions)

        st = s["state"]
        if st == "PACK" and it.kind == "pack":
            return self._pack(conn, s, it.pack_label, actions)
        if st in ("PACK", "QTY") and it.kind == "quantity":
            if st == "PACK":
                return TurnResult(st, [Msg("text", "पहले पैक साइज़ चुन लीजिए 👇"), self._pack_msg(self.cat.get(s["product_key"]))], actions)
            return self._qty(conn, s, it.quantity, actions)
        if st == "ADDRESS_CONFIRM":
            if it.kind == "yes":
                return self._address_confirmed(conn, s, actions)
            if it.kind in ("no", "change_address"):
                s["state"] = "ADDRESS_COLLECT"; s["address_confirmed"] = 0; _save(conn, s)
                return TurnResult("ADDRESS_COLLECT", [self._address_prompt()], actions)
            if it.kind == "later":
                s["state"] = "PRODUCT"; _save(conn, s)
                return TurnResult("PRODUCT", [Msg("text", "ठीक है जी 🙏 जब भी तैयार हों, बस *ORDER* लिखें — आपकी पसंद याद रखी है।")], actions)
        if st == "ADDRESS_COLLECT":
            return self._address_collect(conn, s, text_in, actions)
        if st == "QUOTE":
            if it.kind == "yes":
                return self._create_payment(conn, s, actions)
            if it.kind in ("no", "change_address"):
                s["state"] = "ADDRESS_COLLECT" if it.kind == "change_address" else "PACK"; _save(conn, s)
                return TurnResult(s["state"], [self._address_prompt() if it.kind == "change_address" else self._pack_msg(self.cat.get(s["product_key"]))], actions)
        if st == "AWAITING_PAYMENT":
            if it.kind == "yes":
                return self._create_payment(conn, s, actions)
            return TurnResult(st, [Msg("buttons", f"आपका भुगतान लिंक अभी मान्य है 👇\n{s['invoice_url']}\nभुगतान पूरा होते ही आपको ऑर्डर नंबर अपने-आप मिल जाएगा।",
                                        [("human", "व्यक्ति से बात करें"), ("addr_change", "पता बदलना है")])], actions)
        # unknown in any other state: re-prompt current step
        _save(conn, s)
        return TurnResult(st, [Msg("text", "माफ़ कीजिए, समझ नहीं पाई 🙏 नीचे से चुनिए या *व्यक्ति* लिखें।"), self._menu()], actions)

    # ---------------- steps ----------------
    def _menu(self) -> Msg:
        rows = [(f"product:{p.key}", (p.title_hi if p.sellable else p.title_hi)[:24], ("ताज़ा कीमत और पैक" if p.sellable else "जानकारी / टीम संपर्क")[:72])
                for p in self.cat.products.values() if p.approved_for_sale or p.delivery_class == "registry"]
        rows += [("track", "📦 ऑर्डर ट्रैक करें", "ऑर्डर नंबर से स्टेटस"), ("human", "👤 व्यक्ति से बात करें", "हमारी टीम जवाब देगी")]
        return Msg("list", "🙏 राम राम जी! Amrit Dairy में आपका स्वागत है।\nआज आपको क्या चाहिए? 👇", rows=rows[:10])

    def _product(self, conn, s, it, actions) -> TurnResult:
        p: Product | None = self.cat.get(it.product_key)
        if not p:
            return TurnResult(s["state"], [self._menu()], actions)
        if p.sellable and not self.cat.refresh_live(self.shop, self.price_ttl_s):
            actions.append("catalog:stale")          # live prices unavailable -> do not quote from the snapshot
            s["state"] = "PRODUCT"; s["product_key"] = p.key; _save(conn, s)
            return TurnResult("PRODUCT", [Msg("buttons", f"{p.title_hi} — {p.short_hi}\nताज़ा कीमत अभी नहीं मिल पा रही 🙏 कुछ मिनट बाद फिर लिखें या टीम से पूछें।",
                                               [("human", "टीम से बात करें"), ("menu", "मुख्य मेनू")])], actions)
        lead_id, created = leads.upsert_lead(conn, self.brand, s["contact_id"], p.key, "enquiry")
        actions += [f"lead:{'created' if created else 'updated'}:{lead_id}", f"label:lead_{p.key}"]
        if p.delivery_class == "registry":
            s["state"] = "HANDOFF"; _save(conn, s)
            return TurnResult("HANDOFF", [Msg("buttons", "🐄 गाय ख़रीद/बिक्री के लिए हमारी Cow Desk टीम आपसे बात करेगी। नस्ल/दूध/स्वास्थ्य की पुष्टि जाँच के बाद ही होती है।",
                                               [("human", "टीम से बात करें"), ("menu", "मुख्य मेनू")])], actions, True, "Cow Desk")
        if p.delivery_class == "local_only" or not p.approved_for_sale:
            s["state"] = "PRODUCT"; s["product_key"] = p.key; _save(conn, s)
            return TurnResult("PRODUCT", [Msg("buttons", f"{p.title_hi}: {p.short_hi}\nयह अभी ऑनलाइन उपलब्ध नहीं है — हमारी टीम उपलब्धता बताएगी।",
                                               [("human", "टीम से पूछें"), ("menu", "मुख्य मेनू")])], actions)
        if not p.sellable:
            s["state"] = "PRODUCT"; s["product_key"] = p.key; _save(conn, s)
            return TurnResult("PRODUCT", [Msg("buttons", f"{p.title_hi} — {p.short_hi}\nताज़ा कीमत और पैक की पुष्टि हमारी टीम करेगी (कैटलॉग सिंक लंबित)।",
                                               [("human", "टीम से बात करें"), ("menu", "मुख्य मेनू")])], actions + ["catalog:unverified"])
        s["product_key"] = p.key; s["state"] = "PACK"; s["variant_id"] = None; s["quantity"] = None
        if s.get("shopify_order_id") or s.get("draft_order_id"):   # new purchase after a completed/abandoned one: fresh quote
            for k in ("quote_json", "quote_ref", "draft_order_id", "invoice_url", "shopify_order_id", "shopify_order_name", "payment_id"):
                s[k] = None
            s["address_confirmed"] = 0
        msgs: list[Msg] = []
        if p.media_approved and p.video_url:          # native WhatsApp video (approved MP4 from Shopify media)
            msgs.append(Msg("video", f"{p.title_hi} 🎥", media_url=p.video_url))
        elif self.cat.brand_video_url:                # owner's published brand video as a link (YouTube cannot be sent as media)
            msgs.append(Msg("text", f"🎥 हमारा फ़ार्म और {p.title_hi} कैसे बनता है — देखिए:\n{self.cat.brand_video_url}"))
        msgs.append(Msg("text", f"🧈 *{p.title_hi}*\n{p.short_hi}"))
        msgs.append(self._pack_msg(p))
        _save(conn, s)
        leads.upsert_lead(conn, self.brand, s["contact_id"], p.key, "product_selected")
        r = TurnResult("PACK", msgs, actions)
        if it.pack_label:  # "2 ghee 5 L" style — fast path
            r2 = self._pack(conn, s, it.pack_label, actions)
            if it.quantity and r2.state == "QTY":
                return self._qty(conn, s, it.quantity, actions)
            return r2
        return r

    def _pack_msg(self, p: Product) -> Msg:
        rows = [(f"pack:{v.label}", f"{v.label_hi} — {_inr(v.net_minor)}"[:24],
                 ((f"MRP {_inr(v.price_minor)}, {_inr(v.offer_minor)} छूट • " if v.offer_minor else "") + ("उपलब्ध" if v.available else "स्टॉक पुष्टि लंबित"))[:72])
                for v in p.variants if v.variant_id and v.price_minor]
        return Msg("list", "पैक साइज़ चुनिए 👇 (कीमत टैक्स सहित, कूरियर मुफ़्त*)", rows=rows)

    def _pack(self, conn, s, pack_label, actions) -> TurnResult:
        p = self.cat.get(s["product_key"])
        v = next((v for v in p.variants if _norm_label(v.label) == _norm_label(pack_label or "")), None)
        if not v or not v.variant_id:
            return TurnResult("PACK", [Msg("text", "यह पैक उपलब्ध नहीं है — नीचे से चुनिए 👇"), self._pack_msg(p)], actions)
        if v.available is False:
            return TurnResult("PACK", [Msg("text", f"{v.label_hi} अभी स्टॉक में नहीं है 🙏 दूसरा पैक चुनिए:"), self._pack_msg(p)], actions)
        s["variant_id"] = v.variant_id; s["state"] = "QTY"; _save(conn, s)
        price_line = f"{_inr(v.net_minor)} (MRP {_inr(v.price_minor)}, {_inr(v.offer_minor)} WhatsApp छूट)" if v.offer_minor else _inr(v.price_minor)
        return TurnResult("QTY", [Msg("buttons", f"{p.title_hi} {v.label_hi} — {price_line}\nकितने पैक चाहिए?", [("qty:1", "1"), ("qty:2", "2"), ("qty:3", "3")])], actions)

    def _qty(self, conn, s, qty, actions) -> TurnResult:
        if not (1 <= qty <= 20):
            return TurnResult("QTY", [Msg("text", "1 से 20 के बीच संख्या लिखिए 🙏")], actions)
        s["quantity"] = qty
        cust = self.shop.find_customer_by_phone(s["phone_e164"])
        addrs = cust.addresses if cust else []
        s["customer_id"] = cust.id if cust else None
        if len(addrs) == 1:
            s["address_json"] = json.dumps(addrs[0].__dict__); s["state"] = "ADDRESS_CONFIRM"; _save(conn, s)
            return TurnResult("ADDRESS_CONFIRM", [Msg("buttons", f"Aapka pichhla delivery address hamare paas saved hai:\n📍 {addrs[0].summary_hi()}\nKya aapka order isi address par bhejein?",
                                                       [("addr_yes", "Haan, isi address par"), ("addr_change", "Address change karna hai"), ("addr_later", "Baad mein")])], actions)
        if len(addrs) > 1:
            s["state"] = "ADDRESS_CONFIRM"; s["address_json"] = json.dumps([a.__dict__ for a in addrs]); _save(conn, s)
            rows = [(f"addr:{i}", a.city[:24], a.summary_hi()[:72]) for i, a in enumerate(addrs[:8])] + [("addr_change", "नया पता", "दूसरा पता भेजें")]
            return TurnResult("ADDRESS_CONFIRM", [Msg("list", "आपके कई पते saved हैं — किस पर भेजें?", rows=rows)], actions)
        s["state"] = "ADDRESS_COLLECT"; _save(conn, s)
        return TurnResult("ADDRESS_COLLECT", [self._address_prompt()], actions)

    def _address_prompt(self) -> Msg:
        return Msg("text", "🏠 पूरा Delivery Address एक ही message में भेजें:\nनाम, मकान/गली, एरिया/गाँव, शहर, ज़िला, राज्य, PIN (6 अंक), मोबाइल")

    def _address_collect(self, conn, s, text_in, actions) -> TurnResult:
        a = parse_address(text_in, s["phone_e164"])
        if not a:
            return TurnResult("ADDRESS_COLLECT", [Msg("text", "पता अधूरा लगा 🙏 कृपया 6-अंकों का PIN और शहर/ज़िला ज़रूर लिखें।"), self._address_prompt()], actions)
        s["address_json"] = json.dumps(a.__dict__); s["state"] = "ADDRESS_CONFIRM"; _save(conn, s)
        return TurnResult("ADDRESS_CONFIRM", [Msg("buttons", f"यह पता सही है?\n📍 {a.summary_hi()}", [("addr_yes", "हाँ, सही है"), ("addr_change", "बदलना है")])], actions)

    def _address_confirmed(self, conn, s, actions) -> TurnResult:
        raw = json.loads(s["address_json"])
        if isinstance(raw, list):
            raw = raw[0]
        a = Address(**{k: raw[k] for k in Address.__dataclass_fields__ if k in raw})
        if a.country.lower() not in ("india", "in"):
            return TurnResult("ADDRESS_COLLECT", [Msg("text", "फ़िलहाल भारत के बाहर डिलीवरी नहीं है 🙏 भारत का पता भेजें या `EXPORT` लिखकर प्रतीक्षा-सूची में जुड़ें।")], actions + ["label:export_waitlist"])
        s["address_confirmed"] = 1
        p = self.cat.get(s["product_key"]); v = next(v for v in p.variants if v.variant_id == s["variant_id"])
        qty = s["quantity"]; discount = v.offer_minor * qty   # owner-approved channel offer only
        # Final amount comes from Shopify's own calculation for these exact lines + address + discount (price, GST, shipping rate, stock)
        try:
            calc = self.shop.calculate_quote(variant_id=v.variant_id, quantity=qty, address=a, customer_id=s.get("customer_id"),
                                             discount_minor=discount, discount_title=v.offer_title)
        except Exception as exc:
            audit(conn, "engine", "quote.calc_failed", s["session_id"], {"err": f"{type(exc).__name__}"})
            s["state"] = "HANDOFF"; _save(conn, s)
            return TurnResult("HANDOFF", [Msg("text", "कीमत की पुष्टि अभी नहीं हो पाई 🙏 हमारी टीम सही राशि के साथ लिंक भेजेगी।")], actions + ["case:payment_review"], True, "Payment Review")
        if not calc.available or (calc.inventory_policy == "DENY" and calc.inventory_qty is not None and calc.inventory_qty < qty):
            v.available = False if not calc.available else v.available
            s["state"] = "PACK"; _save(conn, s)
            return TurnResult("PACK", [Msg("text", f"{v.label_hi} अभी {'स्टॉक में नहीं' if calc.available else 'उपलब्ध नहीं'} है 🙏 दूसरा पैक चुनिए:"), self._pack_msg(p)], actions + ["stock:insufficient"])
        if calc.shipping_handle is None:
            s["state"] = "ADDRESS_COLLECT"; _save(conn, s)
            return TurnResult("ADDRESS_COLLECT", [Msg("text", "इस पते/PIN पर अभी डिलीवरी उपलब्ध नहीं दिख रही 🙏 दूसरा पता भेजें या *व्यक्ति* लिखें।")], actions + ["shipping:no_rate"])
        if calc.unit_minor != v.price_minor:          # snapshot was stale: adopt Shopify's price, never ours
            audit(conn, "engine", "price.refreshed", s["session_id"], {"old": v.price_minor, "live": calc.unit_minor})
            v.price_minor = calc.unit_minor; actions.append("price:refreshed")
        if calc.warnings:
            audit(conn, "engine", "quote.warnings", s["session_id"], {"w": calc.warnings[:5]})
        sub = calc.subtotal_minor + calc.discount_minor if calc.subtotal_minor + calc.discount_minor == calc.unit_minor * qty else calc.unit_minor * qty
        quote = {"variant_id": v.variant_id, "label": v.label, "unit_minor": calc.unit_minor, "qty": qty, "subtotal_minor": sub,
                 "shipping_minor": calc.shipping_minor, "shipping_handle": calc.shipping_handle, "shipping_title": calc.shipping_title,
                 "discount_minor": calc.discount_minor, "discount_title": v.offer_title if calc.discount_minor else None,
                 "tax_minor": calc.tax_minor, "taxes_included": calc.taxes_included,
                 "total_minor": calc.total_minor, "currency": calc.currency, "source": "shopify_calculate",
                 "tax_note": "कीमत में GST शामिल" if calc.taxes_included else "GST अतिरिक्त"}
        s["quote_json"] = json.dumps(quote); s["quote_ref"] = s["quote_ref"] or f"AMQ-{uuid.uuid4().hex[:10].upper()}"; s["state"] = "QUOTE"; _save(conn, s)
        leads.upsert_lead(conn, self.brand, s["contact_id"], p.key, "address_confirmed")
        disc_line = f"WhatsApp छूट: −{_inr(calc.discount_minor)}\n" if calc.discount_minor else ""
        tax_line = f"(इसमें GST {_inr(calc.tax_minor)} शामिल)" if calc.taxes_included and calc.tax_minor else (f"+ GST {_inr(calc.tax_minor)}" if calc.tax_minor else "")
        ship_line = "मुफ़्त" if calc.shipping_minor == 0 else f"{_inr(calc.shipping_minor)} ({calc.shipping_title})"
        txt = (f"🧾 *ऑर्डर सारांश*\n{p.title_hi} {v.label_hi} × {qty} = {_inr(sub)}\n{disc_line}कूरियर: {ship_line}\n"
               f"*कुल: {_inr(calc.total_minor)}* {tax_line}\n📍 {a.summary_hi()}\nभुगतान का सुरक्षित लिंक भेजें?")
        return TurnResult("QUOTE", [Msg("buttons", txt, [("confirm_yes", "✅ हाँ, लिंक भेजें"), ("addr_change", "पता बदलें"), ("confirm_no", "पैक बदलें")])], actions)

    def _create_payment(self, conn, s, actions) -> TurnResult:
        # idempotent on quote_ref: an existing draft is reused, never duplicated
        existing = conn.execute(text("SELECT draft_order_id FROM draft_order_ref WHERE quote_ref=:q"), {"q": s["quote_ref"]}).fetchone()
        if existing:
            s["state"] = "AWAITING_PAYMENT"; _save(conn, s)
            return TurnResult("AWAITING_PAYMENT", [Msg("text", f"आपका भुगतान लिंक 👇\n{s['invoice_url']}")], actions + ["draft:reused"])
        raw = json.loads(s["address_json"]); raw = raw[0] if isinstance(raw, list) else raw
        a = Address(**{k: raw[k] for k in Address.__dataclass_fields__ if k in raw})
        q = json.loads(s["quote_json"])
        cust = self.shop.find_customer_by_phone(s["phone_e164"])
        d = self.shop.create_draft_order(variant_id=q["variant_id"], quantity=q["qty"], address=a, phone=s["phone_e164"], quote_ref=s["quote_ref"],
                                         customer_id=cust.id if cust else None, note=f"WhatsApp engine quote {s['quote_ref']}",
                                         discount_minor=int(q.get("discount_minor") or 0), discount_title=q.get("discount_title"),
                                         shipping_handle=q.get("shipping_handle"))
        if d.total_minor != q["total_minor"] or d.currency != q["currency"]:
            audit(conn, "engine", "quote.mismatch", s["session_id"], {"quoted": q["total_minor"], "shopify": d.total_minor})
            s["state"] = "HANDOFF"; _save(conn, s)
            return TurnResult("HANDOFF", [Msg("text", "कीमत की पुष्टि में अंतर आया — हमारी टीम सही राशि के साथ लिंक भेजेगी 🙏")], actions + ["case:payment_review"], True, "Payment Review")
        conn.execute(text("INSERT INTO draft_order_ref(draft_order_id, session_id, quote_ref, created_at) VALUES (:d,:s,:q,:n)"),
                     {"d": d.id, "s": s["session_id"], "q": s["quote_ref"], "n": now_iso()})
        s["draft_order_id"] = d.id; s["invoice_url"] = d.invoice_url; s["state"] = "AWAITING_PAYMENT"; _save(conn, s)
        leads.upsert_lead(conn, self.brand, s["contact_id"], s["product_key"], "payment_pending")
        return TurnResult("AWAITING_PAYMENT", [Msg("text", f"🔒 सुरक्षित भुगतान लिंक (UPI / कार्ड / नेट-बैंकिंग):\n{d.invoice_url}\nकुल {_inr(d.total_minor)} • लिंक 48 घंटे मान्य\n"
                                                           "भुगतान सफल होते ही ऑर्डर नंबर अपने-आप यहीं मिलेगा — screenshot भेजने की ज़रूरत नहीं।")],
                          actions + [f"draft:{d.id}", "label:payment_pending"])

    def _testpay(self, conn, s, actions) -> TurnResult:
        if s["phone_e164"] not in self.test_allowlist:
            return TurnResult(s["state"], [self._menu()], actions + ["testpay:denied"])
        existing = conn.execute(text("SELECT draft_order_id FROM draft_order_ref WHERE quote_ref=:q"), {"q": s.get("quote_ref") or ""}).fetchone()
        if existing and s.get("state") == "AWAITING_PAYMENT" and (s.get("quote_ref") or "").startswith("AMQ-TEST-"):
            return TurnResult("AWAITING_PAYMENT", [Msg("text", f"Test link (₹{self.test_payment_minor // 100}) 👇\n{s['invoice_url']}")], actions + ["draft:reused"])
        cust = self.shop.find_customer_by_phone(s["phone_e164"])
        addr = (cust.addresses[0] if cust and cust.addresses else Address("Engine Test", "Amrit Dairy, Rajbhaya Garhi Brahmanan", "Sonipat", "Haryana", "131001", s["phone_e164"]))
        for k in ("draft_order_id", "invoice_url", "shopify_order_id", "shopify_order_name", "payment_id"):
            s[k] = None
        s["quote_ref"] = f"AMQ-TEST-{uuid.uuid4().hex[:8].upper()}"; s["product_key"] = None; s["variant_id"] = None; s["quantity"] = 1
        s["address_json"] = json.dumps(addr.__dict__)
        s["quote_json"] = json.dumps({"variant_id": None, "label": "gateway-test", "unit_minor": self.test_payment_minor, "qty": 1, "subtotal_minor": self.test_payment_minor,
                                      "shipping_minor": 0, "discount_minor": 0, "discount_title": None, "total_minor": self.test_payment_minor, "currency": "INR", "tax_note": "test"})
        d = self.shop.create_test_draft(amount_minor=self.test_payment_minor, address=addr, phone=s["phone_e164"], quote_ref=s["quote_ref"], customer_id=cust.id if cust else None)
        if d.total_minor != self.test_payment_minor:
            s["state"] = "HANDOFF"; _save(conn, s)
            return TurnResult("HANDOFF", [Msg("text", "test draft total mismatch")], actions + ["case:payment_review"], True, "Payment Review")
        conn.execute(text("INSERT INTO draft_order_ref(draft_order_id, session_id, quote_ref, created_at) VALUES (:d,:s,:q,:n)"),
                     {"d": d.id, "s": s["session_id"], "q": s["quote_ref"], "n": now_iso()})
        s["draft_order_id"] = d.id; s["invoice_url"] = d.invoice_url; s["state"] = "AWAITING_PAYMENT"; _save(conn, s)
        audit(conn, "engine", "testpay.created", s["session_id"], {"draft": d.id, "amount_minor": d.total_minor})
        return TurnResult("AWAITING_PAYMENT", [Msg("text", f"🧪 GATEWAY TEST — ₹{self.test_payment_minor // 100} (कोई सामान नहीं भेजा जाएगा)\n{d.invoice_url}\n"
                                                           "भुगतान के बाद पुष्टि अपने-आप आएगी — यही असली gateway/webhook test है।")], actions + [f"draft:{d.id}", "testpay:created"])

    def _paid_claim(self, conn, s, actions) -> TurnResult:
        if s["state"] == "ORDERED":
            return TurnResult("ORDERED", [Msg("text", f"जी, आपका भुगतान मिल चुका है ✅ ऑर्डर {s['shopify_order_name']}।")], actions)
        return TurnResult(s["state"], [Msg("text", "धन्यवाद 🙏 भुगतान की पुष्टि हमें बैंक/गेटवे से अपने-आप मिलती है — मिलते ही ऑर्डर नंबर यहीं आएगा। "
                                               "अगर 15 मिनट में न आए तो *व्यक्ति* लिखें, टीम Razorpay में जाँच करेगी।")], actions + ["label:payment_verification_pending"])

    def _track(self, conn, s, actions) -> TurnResult:
        if s.get("shopify_order_name"):
            return TurnResult(s["state"], [Msg("text", f"📦 ऑर्डर {s['shopify_order_name']}: स्थिति की जानकारी dispatch के बाद tracking लिंक के साथ आएगी।")], actions)
        return TurnResult(s["state"], [Msg("text", "📦 अपना ऑर्डर नंबर (जैसे #1150) भेजें — हम स्थिति बताएँगे।")], actions + ["label:order_intent"])


def _numbered_choice(text_in: str, last_options_json: str | None) -> str | None:
    t = (text_in or "").strip().translate(str.maketrans("०१२३४५६७८९", "0123456789"))
    if not last_options_json or not t.isdigit():
        return None
    opts = json.loads(last_options_json)
    n = int(t)
    return opts[n - 1] if 1 <= n <= len(opts) else None


def render_plain(messages: list[Msg]) -> str:
    """One WhatsApp text body for channels that cannot send interactive messages: options become numbered lines."""
    out: list[str] = []
    for m in messages:
        body = m.text
        if m.media_url:
            body = f"{body}\n{m.media_url}"
        opts = [t for _, t in m.buttons] + [f"{t} — {d}" if d else t for _, t, d in m.rows]
        if opts:
            body += "\n" + "\n".join(f"{i + 1}️⃣ {o}" for i, o in enumerate(opts)) + "\n(नंबर लिखकर भेजें)"
        out.append(body)
    return "\n\n".join(out)


def parse_address(text_in: str, phone: str) -> Address | None:
    import re
    t = (text_in or "").strip()
    pin = re.search(r"\b(\d{6})\b", t)
    if not pin or len(t) < 20:
        return None
    parts = [x.strip() for x in re.split(r"[,\n]+", t) if x.strip()]
    if len(parts) < 3:
        return None
    name = parts[0]
    city = next((x for x in parts[1:] if not re.search(r"\d{6}", x)), parts[1])
    province = "Haryana" if re.search(r"haryana|हरियाणा", t, re.I) else ("Delhi" if re.search(r"delhi|दिल्ली", t, re.I) else "")
    mob = re.search(r"\b([6-9]\d{9})\b", t)
    return Address(name=name, address1=", ".join(parts[1:-1])[:200] or parts[1], city=city[:60], province=province or city[:60], zip=pin.group(1),
                   phone=("+91" + mob.group(1)) if mob else phone)


def quote_ref_from_order(order: dict[str, Any]) -> str | None:
    """quote_ref from a Shopify order payload (webhook REST shape: note_attributes/tags; GraphQL shape: customAttributes)."""
    for a in (order.get("note_attributes") or order.get("customAttributes") or []):
        if (a.get("name") or a.get("key")) == "quote_ref" and a.get("value"):
            return str(a["value"])
    tags = order.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return next((t.split(":", 1)[1] for t in tags if t.startswith("quote:")), None)


# ---------------- payment completion (called from the verified orders/paid webhook) ----------------
def on_order_paid(conn: Connection, shopify_order: dict[str, Any], shopify: ShopifyClient) -> dict[str, Any]:
    """Idempotent: links the Shopify order to the draft/session once; a replayed webhook returns 'duplicate'."""
    draft_gid = shopify_order.get("draft_order_id") or (shopify_order.get("draft_order") or {}).get("admin_graphql_api_id")
    ref = None
    if draft_gid:
        ref = conn.execute(text("SELECT * FROM draft_order_ref WHERE draft_order_id=:d"), {"d": draft_gid}).fetchone()
    if not ref:
        # Shopify's orders/paid payload carries no draft id, but the draft's custom attribute quote_ref and the
        # tag quote:<ref> are copied onto the real order (verified live on #1151, 12 Oct 2026).
        quote_ref = quote_ref_from_order(shopify_order)
        if quote_ref:
            ref = conn.execute(text("SELECT * FROM draft_order_ref WHERE quote_ref=:q"), {"q": quote_ref}).fetchone()
    if not ref:
        return {"status": "unlinked", "reason": "order carries no engine draft/quote reference"}
    draft_gid = ref.draft_order_id
    if ref.completed_order_id:
        return {"status": "duplicate", "order_id": ref.completed_order_id}
    # verify against Shopify (authenticated), never trust the webhook body alone for money
    verified = shopify.get_order(shopify_order["id"]) or {}
    if (verified.get("displayFinancialStatus") or "").upper() != "PAID":
        return {"status": "not_paid", "financial": verified.get("displayFinancialStatus")}
    s = dict(conn.execute(text("SELECT * FROM conversation_session WHERE session_id=:s"), {"s": ref.session_id}).fetchone()._mapping)
    q = json.loads(s["quote_json"] or "{}")
    amt = int(round(float(verified["currentTotalPriceSet"]["shopMoney"]["amount"]) * 100))
    li = (verified.get("lineItems") or {}).get("nodes") or []
    li_variant = (li[0].get("variant") or {}).get("id") if li else None
    if amt != q.get("total_minor") or verified["currentTotalPriceSet"]["shopMoney"]["currencyCode"] != q.get("currency") \
            or not li or li_variant != q.get("variant_id") or li[0]["quantity"] != q.get("qty"):
        audit(conn, "engine", "order.mismatch", ref.session_id, {"amt": amt, "quote": q})
        return {"status": "mismatch", "order_id": verified["id"]}
    conn.execute(text("UPDATE draft_order_ref SET completed_order_id=:o, completed_at=:n WHERE draft_order_id=:d"),
                 {"o": verified["id"], "n": now_iso(), "d": draft_gid})
    s["shopify_order_id"] = verified["id"]; s["shopify_order_name"] = verified["name"]; s["state"] = "ORDERED"; _save(conn, s)
    if s.get("product_key"):
        leads.upsert_lead(conn, s["brand_id"], s["contact_id"], s["product_key"], "ordered")
    msg = Msg("text", f"✅ भुगतान मिल गया, धन्यवाद जी!\nआपका ऑर्डर *{verified['name']}* confirm हो गया — {_inr(amt)}\nDispatch होते ही tracking इसी WhatsApp पर आएगी।")
    return {"status": "ordered", "order_id": verified["id"], "order_name": verified["name"], "message": msg.as_dict(), "phone": s["phone_e164"]}
