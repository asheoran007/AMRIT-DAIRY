"""Automation V2 (config/automation_v2.json) is implemented by the engine: this walks the whole V2 journey on the test
number with mock transport and checks stage order, single reply per turn, masked saved address before confirmation,
lead tags, Hindi/English/Hinglish entry, opt-out and handoff."""
import json
from pathlib import Path

from sqlalchemy import text

import pytest

from amrit_adapter.engine.shopify_client import FakeShopifyClient
from amrit_adapter.engine.state import Engine, on_order_paid
from amrit_adapter.engine.v2_map import stage_for
from tests.conftest import BRAND
from tests.test_engine_sequence import PHONE, saved_customer, verified_catalog


@pytest.fixture()
def eng(engine, tmp_path):
    shop = FakeShopifyClient({PHONE: saved_customer()})
    return Engine(verified_catalog(tmp_path), shop, BRAND), shop


def test_catalogue_file_is_consistent():
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "automation_v2.json").read_text(encoding="utf-8"))
    ids = [s["id"] for s in cfg["stages"]]
    assert ids == [f"AD-V2-{i:02d}" for i in range(16)] and "INACTIVE" in cfg["_status"]


def test_v2_journey_stage_sequence_single_reply_masked_address_and_tags(engine, eng):
    e, shop = eng
    seen = []
    with engine.begin() as c:
        def turn(txt=None, button=None):
            r = e.turn(c, PHONE, txt or "", button)
            seen.append(stage_for(r, c.execute(text("SELECT product_key FROM conversation_session WHERE phone_e164=:p"), {"p": PHONE}).scalar()))
            assert 1 <= len(r.messages) <= 3          # one engine answer per inbound message (may be text + media + buttons)
            return r
        r = turn("namaste")                                   # AD-V2-00 welcome/menu
        r = turn("ghee ka rate kya hai")                      # AD-V2-01 (Hinglish)
        assert "label:lead_ghee" in r.actions
        r = turn(button="pack:5 L")                            # AD-V2-03
        r = turn(button="qty:1")                               # AD-V2-04 masked saved address
        txt = r.messages[0].text
        assert "H.No 12, Sec…" in txt and "Sector 14" not in txt   # masked until confirmed
        r = turn("haan")                                       # AD-V2-06 quote shows the full confirmed address
        assert "H.No 12, Sector 14" in r.messages[0].text and "₹17,000" in r.messages[0].text
        r = turn(button="confirm_yes")                         # AD-V2-07 payment link
        draft = next(a for a in r.actions if a.startswith("draft:")).split(":", 1)[1]
        r = turn("payment kar diya")                           # AD-V2-08 claim -> verification pending, no state change
        assert r.state == "AWAITING_PAYMENT"
        order = shop.complete_checkout(draft, 3100)
        res = on_order_paid(c, {"id": order["id"], "draft_order_id": draft}, shop)   # AD-V2-09
        assert res["status"] == "ordered"
        r = turn("track")                                      # AD-V2-10
        r = turn("STOP")                                       # AD-V2-15
        r = turn("agent se baat karni hai")                    # AD-V2-13
        assert r.handoff
        assert seen == ["AD-V2-00", "AD-V2-01", "AD-V2-03", "AD-V2-04", "AD-V2-06", "AD-V2-07", "AD-V2-08", "AD-V2-10", "AD-V2-15", "AD-V2-13"]
        # AD-V2-14: one open lead per (contact, product), stage moved forward to 'ordered'
        assert c.execute(text("SELECT stage FROM lead WHERE lead_type='product:ghee'")).fetchall() == [("ordered",)]


def test_v2_english_and_hindi_entries_route_to_the_same_stage(engine, eng):
    e, shop = eng
    with engine.begin() as c:
        assert stage_for(e.turn(c, PHONE, "I want to buy ghee"), "ghee") == "AD-V2-01"
        assert stage_for(e.turn(c, "+919999999999", "घी चाहिए"), "ghee") == "AD-V2-01"
        assert stage_for(e.turn(c, "+919999999998", "shahad"), "honey") == "AD-V2-02"
