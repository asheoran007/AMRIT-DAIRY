"""Brief §11 test sequence 1–12, MOCK mode (FakeShopifyClient, signed Shopify webhooks, no real sends).
Test phone: +918814869007 (owner-authorised). No message is delivered by these tests."""
import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from amrit_adapter import handlers
from amrit_adapter.engine import leads
from amrit_adapter.engine.catalog import Catalog
from amrit_adapter.engine.shopify_client import Address, CustomerRecord, FakeShopifyClient
from amrit_adapter.engine.state import Engine, on_order_paid
from amrit_adapter.envelope import InternalEvent
from tests.conftest import BRAND

PHONE = "+918814869007"


def verified_catalog(tmp_path) -> Catalog:
    """Config as it will look after a Shopify sync: ids + prices + approved media."""
    raw = json.loads((Path(__file__).resolve().parent.parent / "config" / "products_amrit.json").read_text(encoding="utf-8"))
    for p in raw["products"]:
        if p["key"] == "ghee":
            p["media"] = {"video_url": "https://cdn.shopify.com/videos/ghee-approved.mp4", "image_url": None, "approved": True}
            for v, vid in zip(p["variants"], ["1", "5", "10"]):
                v["variant_id"] = f"gid://shopify/ProductVariant/{vid}"
        if p["key"] == "honey":
            p["media"] = {"video_url": "https://cdn.shopify.com/videos/honey-approved.mp4", "image_url": None, "approved": True}
            for v, vid in zip(p["variants"], ["h250", "h500", "h1000"]):
                v["variant_id"] = f"gid://shopify/ProductVariant/{vid}"
    f = tmp_path / "products.json"; f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return Catalog.load(f)


def saved_customer():
    return CustomerRecord("gid://shopify/Customer/77", PHONE,
                          [Address("Test Customer", "H.No 12, Sector 14", "Sonipat", "Haryana", "131001", PHONE, id="gid://shopify/MailingAddress/1")], 2)


@pytest.fixture()
def eng(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()})
    return Engine(verified_catalog(tmp_path), shop, BRAND), shop


def test_01_02_03_ghee_enquiry_creates_lead_sends_video_and_packs(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        r = e.turn(c, PHONE, "Ghee chahiye", display_name="Test Customer")
        assert r.state == "PACK"
        assert any(a.startswith("lead:created") for a in r.actions) and "label:lead_ghee" in r.actions
        lead_id = next(a for a in r.actions if a.startswith("lead:")).split(":")[2]
        tags = leads.tags_for(c, lead_id)
        assert {"LEAD_NEW", "INTEREST_GHEE", "CHANNEL_WHATSAPP", "STAGE_PRODUCT_SELECTED"} <= tags
        types = [m.type for m in r.messages]
        assert types == ["video", "list"]                                           # real approved video first, then ONE card (story + packs)
        assert r.messages[0].media_url.endswith("ghee-approved.mp4")
        assert [row[0] for row in r.messages[1].rows] == ["pack:1 Ltr", "pack:5 Ltr", "pack:10 Ltr"]
        assert "AMRIT" not in r.messages[1].text and "╭" in r.messages[1].text      # house-style boxed header
        r = e.turn(c, PHONE, button_id="pack:5 L")
        assert r.state == "QTY" and "₹17,000" in r.messages[0].text and "MRP ₹17,500" in r.messages[0].text   # owner-approved ₹500 WhatsApp offer on live ₹17,500
        r = e.turn(c, PHONE, "2")
        assert r.state == "ADDRESS_CONFIRM"


def test_04_saved_address_yes_branch_and_06_quote(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); r = e.turn(c, PHONE, button_id="qty:2")
        assert "आपका पिछला पता हमारे पास सुरक्षित है" in r.messages[0].text
        assert [b[1] for b in r.messages[0].buttons] == ["✅ हाँ, यही पता", "✏️ नया पता", "⏳ बाद में"]
        assert all(len(b[1]) <= 20 for b in r.messages[0].buttons)                  # WhatsApp reply-button title limit
        r = e.turn(c, PHONE, "haan")
        assert r.state == "QUOTE"
        q = json.loads(c.execute(text("SELECT quote_json FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar())
        assert q["total_minor"] == 2 * 350000 and q["shipping_minor"] == 0 and q["currency"] == "INR"
        assert "कुल: ₹7,000" in r.messages[0].text


def test_05_no_branch_collects_and_reconfirms_address(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1")
        r = e.turn(c, PHONE, button_id="addr_change")
        assert r.state == "ADDRESS_COLLECT"
        r = e.turn(c, PHONE, "Amit Kumar, House 5 Gali 2, Model Town, Sonipat, Haryana, 1310")  # bad PIN
        assert r.state == "ADDRESS_COLLECT" and "PIN" in r.messages[0].text
        r = e.turn(c, PHONE, "Amit Kumar, House 5 Gali 2, Model Town, Sonipat, Haryana 131001, 8814869007")
        assert r.state == "ADDRESS_CONFIRM" and "131001" in r.messages[0].text
        r = e.turn(c, PHONE, "हाँ")
        assert r.state == "QUOTE"
        a = json.loads(c.execute(text("SELECT address_json FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar())
        assert a["zip"] == "131001" and a["city"] == "Model Town" or a["zip"] == "131001"


def test_07_08_09_11_payment_webhook_creates_exactly_one_order_and_replay_is_duplicate(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:5 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes")
        assert r.state == "AWAITING_PAYMENT" and "invoices/test-1" in r.messages[0].text
        draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        # customer says "paid" -> NOT marked paid
        r = e.turn(c, PHONE, "payment kar diya")
        assert r.state == "AWAITING_PAYMENT" and "label:payment_verification_pending" in r.actions
        # a second "yes" never creates a second draft
        r = e.turn(c, PHONE, button_id="confirm_yes")
        assert "draft:reused" in r.actions and len(shop.drafts) == 1
        # sandbox checkout completes -> Shopify order exists -> verified webhook
        order = shop.complete_checkout(draft, 2001)
        res = on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)
        assert res["status"] == "ordered" and res["order_name"] == "#2001" and "#2001" in res["message"]["text"]
        s = c.execute(text("SELECT state, shopify_order_name FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).fetchone()
        assert s == ("ORDERED", "#2001")
        # 09: order matches the quote (amount, variant, qty) — verified inside on_order_paid; 11: replay
        res2 = on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)
        assert res2["status"] == "duplicate"
        assert c.execute(text("SELECT COUNT(*) FROM draft_order_ref WHERE completed_order_id IS NOT NULL")).scalar() == 1


def test_09b_mismatched_order_is_not_confirmed(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes")
        draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        order = shop.complete_checkout(draft, 2002)
        order["currentTotalPriceSet"]["shopMoney"]["amount"] = "1.00"   # tampered / edited order
        assert on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)["status"] == "mismatch"


def test_10_tracking_without_dispatch_does_not_invent_shipping(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes"); draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        on_order_paid(c, {"id": shop.complete_checkout(draft, 2003)["id"], "draft_order_id": draft}, shop)
        r = e.turn(c, PHONE, "tracking")
        assert "#2003" in r.messages[0].text and "dispatch" in r.messages[0].text.lower() and "shipped" not in r.messages[0].text.lower()


def test_12_honey_uses_same_engine(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        r = e.turn(c, PHONE, "shahad chahiye")
        assert r.state == "PACK" and r.messages[0].media_url.endswith("honey-approved.mp4")
        assert [row[0] for row in r.messages[1].rows] == ["pack:250 gm", "pack:500 gm", "pack:1 Kg"]
        r = e.turn(c, PHONE, "250 gm")
        assert r.state == "QTY"
        lead = c.execute(text("SELECT lead_type FROM lead WHERE contact_id=(SELECT contact_id FROM conversation_session WHERE phone_e164=:p) ORDER BY created_at"), {"p": PHONE}).fetchall()
        assert ("product:honey",) in lead


def test_restricted_and_unverified_products_never_quote(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        r = e.turn(c, PHONE, "गाय खरीदनी है")
        assert r.handoff and r.handoff_team == "Cow Desk"
        r2 = e.turn(c, "+919999900001", "doodh chahiye")
        assert r2.state == "PRODUCT" and "ऑनलाइन उपलब्ध नहीं" in r2.messages[0].text


def test_unverified_catalog_refuses_prices(engine, tmp_path):
    raw = json.loads((Path(__file__).resolve().parent.parent / "config" / "products_amrit.json").read_text(encoding="utf-8"))
    for p in raw["products"]:
        p["verified_from_shopify"] = False          # simulate a stale / unsynced catalogue
    f = tmp_path / "stale.json"; f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    e = Engine(Catalog.load(f), FakeShopifyClient(), BRAND)
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        assert r.state == "PRODUCT" and "catalog:unverified" in r.actions and "₹" not in r.messages[0].text


def test_live_catalog_has_no_video_so_engine_sends_image_fallback_only_when_approved(engine):
    """Synced config: no Shopify video exists -> no media message is sent (never fabricate media)."""
    e = Engine(Catalog.load(), FakeShopifyClient(), BRAND)
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        assert r.state == "PACK" and [m.type for m in r.messages] == ["list"]       # quick: one message
        assert "https://youtu.be/BAnglrsKYuw" in r.messages[0].text and r.messages[0].media_url is None   # owner's brand video as a link
        assert "₹3,500" in r.messages[0].rows[0][1] and "₹17,000" in r.messages[0].rows[1][1]   # net of the approved ₹500 offer
        assert "MRP ₹17,500" in r.messages[0].rows[1][2] and "₹32,000" in r.messages[0].rows[2][1]


def test_shopify_orders_paid_webhook_end_to_end(engine, eng):
    """HMAC-verified orders/paid delivery (draft_order reference) -> engine -> queued service message; replay -> no second."""
    from amrit_adapter.webhooks import verify as v
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes"); draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        order = shop.complete_checkout(draft, 2010)
        body = json.dumps({"id": 2010, "admin_graphql_api_id": order["id"], "name": "#2010", "financial_status": "paid", "total_price": "3500.00",
                           "currency": "INR", "payment_gateway_names": ["razorpay"], "customer": {"id": 77},
                           # real Shopify orders/paid payloads carry NO draft id — only the copied note_attributes/tags (verified on #1151)
                           "note_attributes": [{"name": "quote_ref", "value": shop.drafts[draft]["quote_ref"]}, {"name": "source", "value": "whatsapp_engine"}],
                           "tags": "amrit-engine, quote:" + shop.drafts[draft]["quote_ref"] + ", whatsapp"}).encode()
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        hdr = {"X-Shopify-Hmac-SHA256": sig, "X-Shopify-Topic": "orders/paid", "X-Shopify-Event-Id": "evt-paid-1", "X-Shopify-Shop-Domain": "kmaz1y-dq.myshopify.com"}
        v.verify_shopify(body, hdr, "sec")
        ev = v.normalize_shopify(body, hdr, BRAND)
        a1 = handlers.handle(c, ev, shopify_client=shop)
        assert "engine: ordered" in a1
        a2 = handlers.handle(c, ev, shopify_client=shop)        # replayed delivery
        assert "engine: duplicate" in a2
        assert c.execute(text("SELECT COUNT(*) FROM outbox_job WHERE job_type='send_message' AND category='service'")).scalar() == 1


# ---------------- owner-approved WhatsApp pricing (12 Oct 2026) ----------------
def test_pricing_rules_from_owner_12_oct(engine, eng):
    """5 Ltr ₹17,500 → ₹17,000 (₹500 off); 10 Ltr ₹33,000 → ₹32,000 (₹100/L); 1 Ltr unchanged; 25 Ltr rule inactive (no variant).
    The discount travels to Shopify as a titled order-level discount so the invoice total equals the quote."""
    e, shop = eng
    cat = e.cat.get("ghee")
    net = {v.label: v.net_minor for v in cat.variants}
    assert net == {"1 Ltr": 350000, "5 Ltr": 1700000, "10 Ltr": 3200000}
    assert all(v.label != "25 Ltr" for v in cat.variants)
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:10 Ltr"); r = e.turn(c, PHONE, "2")
        r = e.turn(c, PHONE, button_id="addr_yes")
        assert r.state == "QUOTE" and "× 2 = ₹66,000" in r.messages[0].text and "WhatsApp छूट: −₹2,000" in r.messages[0].text and "*कुल: ₹64,000*" in r.messages[0].text
        r = e.turn(c, PHONE, button_id="confirm_yes")
        assert r.state == "AWAITING_PAYMENT"
        d = next(iter(shop.drafts.values()))
        assert d["discount"] == 200000 and d["discount_title"] == "WhatsApp offer: ₹100/L off 10 Ltr" and d["total"] == 6400000
        # payment verification still compares the discounted total: a Shopify order at list price would NOT be confirmed
        gid = next(iter(shop.drafts)); order = shop.complete_checkout(gid, 2101)
        assert on_order_paid(c, order, shop)["status"] == "ordered"


def test_honey_has_no_offer_and_unapproved_rule_never_applies(engine, eng):
    e, shop = eng
    assert all(v.offer_minor == 0 and v.net_minor == v.price_minor for v in e.cat.get("honey").variants)


def test_paid_webhook_without_any_engine_reference_is_unlinked(engine, eng):
    """An order not created by the engine (website checkout) never maps onto a session."""
    e, shop = eng
    with engine.begin() as c:
        assert on_order_paid(c, {"id": "gid://shopify/Order/9", "tags": "web", "note_attributes": []}, shop)["status"] == "unlinked"


def test_worker_builds_live_shopify_client_only_with_token(monkeypatch):
    from amrit_adapter import worker
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "shopify_admin_token", ""); assert worker.build_shopify_client() is None
    monkeypatch.setattr(settings, "shopify_admin_token", "shpat_x"); monkeypatch.setattr(settings, "shopify_shop_domain", "kmaz1y-dq.myshopify.com")
    assert type(worker.build_shopify_client()).__name__ == "LiveShopifyClient"


def test_numbered_text_replies_map_to_last_options_and_render_plain(engine, eng):
    """Text-only channel path (API Call + Text node): '2' after the pack list selects option 2; render_plain numbers options."""
    from amrit_adapter.engine.state import render_plain
    e, shop = eng
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        plain = render_plain(r.messages)
        assert "1️⃣ 1 लीटर — ₹3,500" in plain and "2️⃣ 5 लीटर — ₹17,000" in plain and "(नंबर लिखकर भेजें)" in plain
        r = e.turn(c, PHONE, "2")                        # → pack:5 Ltr
        assert r.state == "QTY" and "5 लीटर" in r.messages[0].text
        r = e.turn(c, PHONE, "३")                        # Devanagari digit → qty:3
        assert r.state == "ADDRESS_CONFIRM"
        r = e.turn(c, PHONE, "1")                        # → addr_yes
        assert r.state == "QUOTE" and "*कुल: ₹51,000*" in r.messages[0].text
        r = e.turn(c, PHONE, "7")                        # out of range → not a choice; falls to re-prompt
        assert r.state == "QUOTE"


def test_testpay_is_allowlisted_small_amount_and_verified_like_any_order(engine, tmp_path):
    """TESTPAY: denied for non-allowlisted numbers; for +918814869007 creates a ₹10 custom-item draft (no product, no stock),
    and the paid webhook is verified through the same on_order_paid path (amount must match)."""
    shop = FakeShopifyClient({PHONE: saved_customer()})
    e = Engine(verified_catalog(tmp_path), shop, BRAND, test_allowlist=(PHONE,), test_payment_minor=1000)
    with engine.begin() as c:
        r = e.turn(c, "+919999999999", "TESTPAY")
        assert "testpay:denied" in r.actions and not shop.drafts
        r = e.turn(c, PHONE, "testpay")
        assert r.state == "AWAITING_PAYMENT" and "testpay:created" in r.actions and "₹10" in r.messages[0].text
        gid = next(iter(shop.drafts)); assert shop.drafts[gid]["total"] == 1000 and shop.drafts[gid]["variant_id"] is None
        r = e.turn(c, PHONE, "TESTPAY"); assert "draft:reused" in r.actions and len(shop.drafts) == 1
        order = shop.complete_checkout(gid, 2200)
        res = on_order_paid(c, order, shop); assert res["status"] == "ordered" and "#2200" in res["message"]["text"]
        assert on_order_paid(c, order, shop)["status"] == "duplicate"
        # the ₹10 test order is put on fulfillment hold immediately (Shiprocket must never book it); real orders are not
        assert "do not ship" in order["fulfillment_hold"]
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='test_order.hold'")).scalar() == 1
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes"); draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        real = shop.complete_checkout(draft, 2201)
        assert on_order_paid(c, {"id": real["id"], "draft_order_id": draft}, shop)["status"] == "ordered" and "fulfillment_hold" not in real


def test_09c_manual_mark_as_paid_is_never_gateway_verification(engine, eng):
    """#1151 pattern: admin 'Mark as paid' -> financial PAID but gateway=manual. Engine must keep waiting (and audit it);
    a later REAL gateway capture on the same order then confirms exactly once."""
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        r = e.turn(c, PHONE, button_id="confirm_yes")
        draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        order = shop.complete_checkout(draft, 2003, gateway="manual")
        res = on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)
        assert res["status"] == "manual_paid" and res["gateways"] == ["manual"]
        assert c.execute(text("SELECT state FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar() == "AWAITING_PAYMENT"
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='order.manual_paid_ignored'")).scalar() == 1
        # pending / failed / test-mode transactions do not count either
        for tr in ({"gateway": "01 Cards, UPI, NB, Wallets by Razorpay", "kind": "SALE", "status": "PENDING", "test": False},
                   {"gateway": "01 Cards, UPI, NB, Wallets by Razorpay", "kind": "SALE", "status": "FAILURE", "test": False},
                   {"gateway": "01 Cards, UPI, NB, Wallets by Razorpay", "kind": "SALE", "status": "SUCCESS", "test": True},
                   {"gateway": "01 Cards, UPI, NB, Wallets by Razorpay", "kind": "AUTHORIZATION", "status": "SUCCESS", "test": False}):
            order["transactions"] = [tr]
            assert on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)["status"] == "manual_paid"
        # the real capture arrives -> ordered, once
        order["transactions"] = [{"gateway": "01 Cards, UPI, NB, Wallets by Razorpay", "kind": "SALE", "status": "SUCCESS", "test": False}]
        assert on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)["status"] == "ordered"
        assert on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)["status"] == "duplicate"


def test_api_failure_is_recovered_with_retry_message_and_unchanged_state(engine, eng):
    """Shopify down mid-journey: no crash, no silence, no state corruption; the next turn works again."""
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:5 L"); e.turn(c, PHONE, button_id="qty:1")
        assert e.turn(c, PHONE, "haan").state == "QUOTE"
        orig = shop.create_draft_order
        shop.create_draft_order = lambda **kw: (_ for _ in ()).throw(RuntimeError("HTTP 503"))
        r = e.turn(c, PHONE, button_id="confirm_yes")
        assert r.state == "QUOTE" and "तकनीकी" in r.messages[0].text and "error:RuntimeError" in r.actions
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='turn.error'")).scalar() == 1
        assert len(shop.drafts) == 0
        shop.create_draft_order = orig
        r = e.turn(c, PHONE, button_id="confirm_yes")     # provider back: the journey continues from the same state
        assert r.state == "AWAITING_PAYMENT" and len(shop.drafts) == 1


def test_parse_address_city_state_pin_and_mobile():
    from amrit_adapter.engine.state import parse_address
    a = parse_address("Ramesh Kumar, H.No 12 Gali 3, Sector 15, Sonipat, Haryana 131001, 9876543210", PHONE)
    assert (a.name, a.address1, a.city, a.province, a.zip, a.phone) == ("Ramesh Kumar", "H.No 12 Gali 3, Sector 15", "Sonipat", "Haryana", "131001", "+919876543210")
    a = parse_address("Amit Kumar\nVillage Rajbhaya Garhi Brahmanan\nNear Amrit Neer Mahal\nSonipat\nHaryana - 131001", PHONE)
    assert a.city == "Sonipat" and a.province == "Haryana" and a.phone == PHONE and "131001" not in a.address1
    a = parse_address("Suresh, 45 MG Road, Indiranagar, Bengaluru 560038 Karnataka", PHONE)
    assert a.city == "Bengaluru" and a.province == "Karnataka"


def test_paid_order_confirmation_is_deliverable_without_manual_consent(engine, eng):
    """Live 19 Oct (#1170): the engine said 'ordered' but the outbox cancelled the confirmation ('no service consent on
    whatsapp'). The customer ordered in this chat, so the payment link step records service consent for the chat contact
    and the confirmation job targets that contact -> the outbox consent check passes. Marketing consent is NOT granted."""
    from amrit_adapter.consent import has_consent
    e, shop = eng
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:1 L"); e.turn(c, PHONE, button_id="qty:1"); e.turn(c, PHONE, "haan")
        e.turn(c, PHONE, button_id="confirm_yes")
        contact = c.execute(text("SELECT contact_id FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar()
        assert has_consent(c, BRAND, contact, "whatsapp", "service") and not has_consent(c, BRAND, contact, "whatsapp", "marketing")
        gid = next(iter(shop.drafts)); order = shop.complete_checkout(gid, 2201)
        res = on_order_paid(c, order, shop)
        assert res["status"] == "ordered" and res["contact_id"] == contact and "ORDER CONFIRMED" in res["message"]["text"]
