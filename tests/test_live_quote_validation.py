"""Catalogue/checkout risks: the final quote is Shopify-calculated (price, GST, shipping rate, stock); the JSON snapshot is only a bootstrap."""
import json
from pathlib import Path

from amrit_adapter.engine.catalog import Catalog
from amrit_adapter.engine.shopify_client import FakeShopifyClient
from amrit_adapter.engine.state import Engine
from tests.conftest import BRAND
from tests.test_engine_sequence import PHONE, saved_customer, verified_catalog


def _to_quote(e, c):
    e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, button_id="pack:5 Ltr"); e.turn(c, PHONE, "1")
    return e.turn(c, PHONE, button_id="addr_yes")


def test_snapshot_price_is_replaced_by_live_price_before_quoting(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()}); shop.PRICES = dict(shop.PRICES); shop.PRICES["gid://shopify/ProductVariant/5"] = 1800000  # store raised 5 Ltr
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        assert "₹17,500" in r.messages[-1].rows[1][1] and e.cat.get("ghee").variants[1].price_minor == 1800000   # pack list refreshed live (₹18,000 − ₹500)
        r = e.turn(c, PHONE, button_id="pack:5 Ltr"); e.turn(c, PHONE, "1"); r = e.turn(c, PHONE, button_id="addr_yes")
        q = json.loads(c.execute(__import__("sqlalchemy").text("SELECT quote_json FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar())
        assert q["source"] == "shopify_calculate" and q["unit_minor"] == 1800000 and q["total_minor"] == 1750000 and q["taxes_included"] is True
        assert "*कुल: ₹17,500*" in r.messages[0].text and "GST" in r.messages[0].text


def test_paid_shipping_rate_is_shown_and_carried_onto_the_draft(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()}); shop.shipping_minor = 25000
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        r = _to_quote(e, c)
        assert "कूरियर: ₹250 (Standard)" in r.messages[0].text and "*कुल: ₹17,250*" in r.messages[0].text   # 17,500 − 500 + 250
        r = e.turn(c, PHONE, button_id="confirm_yes")
        assert r.state == "AWAITING_PAYMENT" and next(iter(shop.drafts.values()))["total"] == 1725000


def test_no_shipping_rate_for_address_means_no_quote(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()})
    def calc(**kw):
        q = FakeShopifyClient.calculate_quote(shop, **kw); q.shipping_handle = None; return q
    shop.calculate_quote = calc
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        r = _to_quote(e, c)
        assert r.state == "ADDRESS_COLLECT" and "shipping:no_rate" in r.actions and not shop.drafts


def test_out_of_stock_or_unavailable_variant_is_not_quoted(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()}); shop.stock = {"gid://shopify/ProductVariant/5": 0}
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        r = _to_quote(e, c)
        assert r.state == "PACK" and "stock:insufficient" in r.actions
    shop2 = FakeShopifyClient({PHONE: saved_customer()}); shop2.unavailable = {"gid://shopify/ProductVariant/5"}
    e2 = Engine(verified_catalog(tmp_path), shop2, BRAND)
    with engine.begin() as c:
        r = e2.turn(c, "+918814869007", "ghee")
        assert "5 Ltr" in r.messages[-1].rows[1][1]
        r = e2.turn(c, PHONE, button_id="pack:5 Ltr")
        assert r.state == "PACK" and "स्टॉक में नहीं" in r.messages[0].text   # refresh marked it unavailable before any quote


def test_live_refresh_failure_blocks_quoting_from_the_snapshot(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()})
    def boom(ids): raise RuntimeError("shopify down")
    shop.get_variants = boom
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        assert r.state == "PRODUCT" and "catalog:stale" in r.actions and all(m.type != "list" for m in r.messages)
        assert e.cat.refresh_error and e.cat.stale


def test_refresh_respects_ttl(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()}); calls = []
    orig = shop.get_variants
    shop.get_variants = lambda ids: (calls.append(1), orig(ids))[1]
    e = Engine(verified_catalog(tmp_path), shop, BRAND)
    with engine.begin() as c:
        e.turn(c, PHONE, "ghee"); e.turn(c, PHONE, "shahad"); e.turn(c, PHONE, "ghee")
    assert len(calls) == 1
